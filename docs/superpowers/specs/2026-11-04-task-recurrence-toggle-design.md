# Переключатель регулярности задач: дизайн

**Дата:** 2026-11-04
**Статус:** реализовано
**Связанные документы:**
- `docs/superpowers/specs/2026-11-03-task-management-database-design.md`
- `docs/superpowers/plans/2026-11-04-task-recurrence-toggle.md`

## 1. Контекст и цель

Сейчас поддерживается только ежедневная серия (`task_series.frequency = 'daily'`) и
разовые задачи (`tasks.series_id IS NULL`). Нужно добавить третий режим —
периодическую серию с интервалом `N >= 2` дней — и дать пользователю
переключать конкретную задачу между тремя состояниями:

| Состояние | Хранение | Смысл |
| --- | --- | --- |
| `one_off` | `tasks.series_id IS NULL` | разовая задача |
| `regular` | активная серия с `frequency = 'daily'`, `interval_days = 1` | ежедневная серия |
| `periodic` | активная серия с `frequency = 'periodic'`, `interval_days = N >= 2` | каждые N дней |

`PATCH /api/v1/tasks/{id}` должен уметь:
1. выключить регулярность (`recurrence = "one_off"`);
2. включить ежедневную регулярность (`recurrence = "regular"`);
3. включить периодическую регулярность (`recurrence = "periodic"`, `interval_days = N`);
4. изменить интервал уже существующей periodic-серии без повторной передачи
   `recurrence`.

Цель — сделать переключение предсказуемым: сохранить историю, не плодить
дубликаты, не оставлять «висящие» напоминания и не менять завершённые задачи.

## 2. Итоговое поведение

### 2.1. Выключение регулярности

`PATCH /api/v1/tasks/{id}` с `{"recurrence": "one_off"}`:

1. Сначала применяются остальные поля из этого же `PATCH` к текущей задаче.
2. Если задача не завершена и `due_at <= now`, `due_at` переносится на первый
   слот серии строго после сегодняшнего локального дня:
   - для daily — завтра;
   - для periodic — ближайшая дата вида `series.starts_on + k * interval_days`,
     строго большая сегодняшней локальной даты.
   - локальное время задачи сохраняется.
3. Все остальные будущие незавершённые экземпляры серии удаляются физически.
   Текущая задача исключается из удаления.
4. Завершённые и просроченные экземпляры остаются историей.
5. Шаблонные напоминания серии отменяются через
   `cancel_series_reminders(user_id, series_id)`.
6. Серия soft-delete: `deleted_at = now`, `deletion_batch_id`.
7. У текущей задачи `series_id = NULL`.
8. Если серия уже soft-deleted, задача трактуется как разовая: выключение
   регулярности ничего не ломает и не возвращает `404`. При явном
   `recurrence = "one_off"` у такой задачи `series_id` очищается, чтобы ответ
   действительно был `one_off`; сама soft-deleted серия и остальная история
   не меняются.

### 2.2. Включение регулярности

`PATCH /api/v1/tasks/{id}` с `{"recurrence": "regular"}` или
`{"recurrence": "periodic", "interval_days": 3}`:

1. Сначала применяются остальные поля из этого же `PATCH` к текущей задаче.
2. Если задача не завершена и `due_at <= now`, `due_at` переносится по тому же
   правилу следующего слота. Для включения якорь — локальная дата итогового
   `due_at` после применения остальных полей.
3. Создаётся серия из снимка текущей задачи:
   - `title`, `description`, `priority`, `project_id`;
   - текущий набор меток;
   - текущие правила напоминаний;
   - `reminders_enabled` текущей задачи;
   - авторская таймзона (`users.timezone`, при отсутствии — `UTC`);
   - `time_of_day` — локальное время итогового `due_at` в авторской зоне;
   - `starts_on` — локальная дата итогового `due_at`;
   - `ends_on = NULL`;
   - `frequency` и `interval_days` — из запроса.
4. Текущая задача привязывается к серии (`series_id = series.id`) как первый
   экземпляр. Генератор не создаёт дубль на её `due_at`.
5. Завершённая задача остаётся завершённой; серия продолжает генерировать
   будущие экземпляры.
6. Архивный/удалённый проект или несуществующие метки возвращают те же
   `400`/`404`, что и создание серии.
7. При конвертации `one_off -> periodic` шаблонное `scheduled`-напоминание
   получает `interval_days` серии, чтобы не срабатывать в дни без задачи.
   При `one_off -> regular` интервал напоминания сохраняется: задачи
   создаются каждый день, разрыва в днях нет. При явном
   `POST/PATCH /api/v1/task-series` интервал напоминания остаётся тем, что
   передал клиент.
8. Инстанс-напоминания самой текущей задачи не пересоздаются из шаблона:
   они остаются её снимком. Перестройка происходит только если `due_at`
   был перенесён, и тогда используются прежние правила текущей задачи.
9. Если задача ссылается на soft-deleted серию и пользователь снова включает
   регулярность, создаётся новая серия, а `task.series_id` перезаписывается
   на неё. Старая soft-deleted серия и её остальная история сохраняются, но
   текущая задача больше не ссылается на старую серию.

### 2.3. Удаление задачи

`DELETE /api/v1/tasks/{id}`:

| Состояние задачи | Поведение |
| --- | --- |
| Разовая задача | soft-delete только задачи, отменить её доставки |
| Серия уже soft-deleted | soft-delete только задачи |
| Активная задача серии (`completed_at IS NULL` и `due_at > now`) | существующее поведение: soft-delete серии и всех будущих незавершённых экземпляров, включая более ранние, чем удаляемая |
| Завершённая задача серии | soft-delete только задачи, серия и остальные экземпляры не трогаются |
| Просроченная незавершённая задача серии (`due_at <= now`) | soft-delete только задачи, серия и остальные экземпляры не трогаются |

## 3. API-контракт

### 3.1. `PATCH /api/v1/tasks/{id}`

Новые поля:

```json
{"recurrence": "one_off"}
{"recurrence": "regular"}
{"recurrence": "periodic", "interval_days": 3}
{"interval_days": 5}
```

Правила валидации:

- `periodic` требует явный `interval_days >= 2`, даже если задача уже в
  periodic-серии. Для смены только интервала нужно передать `interval_days`
  без `recurrence`.
- `regular` допускает `interval_days` только `null` или `1`; отсутствие поля
  трактуется как `1`.
- `one_off` не допускает `interval_days` даже как `null`.
- `interval_days` без `recurrence` допустим только для существующей
  periodic-серии и требует `interval_days >= 2`.
- `interval_days = 1` без `recurrence` отклоняется: daily-серия не меняет
  интервал.
- `recurrence` в одном `PATCH` с другими полями: сначала поля, потом
  конвертация.
- `recurrence = "one_off"` для уже разовой задачи — обычное обновление полей.
- `recurrence = "regular"`/`"periodic"` для уже серии — обновление серии,
  включая смену `frequency`/`interval_days`.

`TaskResponse` получает поле:

```json
{"recurrence": "one_off" | "regular" | "periodic"}
```

`recurrence` вычисляется по серии, включая soft-deleted:

- `series_id IS NULL` → `one_off`;
- серия `frequency = 'daily'` → `regular`;
- серия `frequency = 'periodic'` → `periodic`;
- если серия soft-deleted, классификация сохраняется, чтобы история не теряла
  режим.

### 3.2. `GET /api/v1/tasks`

Фильтр `recurrence` расширяется:

- `all` — без фильтра;
- `regular` — только задачи daily-серий (`frequency = 'daily'`);
- `periodic` — только задачи periodic-серий (`frequency = 'periodic'`);
- `one_off` — только `series_id IS NULL`.

Soft-deleted серии учитываются в join, чтобы исторические экземпляры не
пропадали из фильтра.

### 3.3. `POST/PATCH /api/v1/task-series`

`SeriesCreate`:

```json
{"frequency": "daily", "interval_days": 1}
{"frequency": "periodic", "interval_days": 3}
```

- `frequency` по умолчанию `daily` для обратной совместимости.
- `daily` допускает только `interval_days = 1` (или отсутствие поля, тогда
  нормализуется в `1`).
- `periodic` требует `interval_days >= 2`.
- Явный `POST/PATCH` не переопределяет интервал шаблонного напоминания.

`SeriesUpdate`:

- можно передать `frequency`, `interval_days` или оба поля;
- `frequency = 'daily'` требует `interval_days` `null`/`1` или отсутствие;
- `frequency = 'periodic'` требует `interval_days >= 2`, если интервал не
  берётся из существующей periodic-серии;
- `interval_days` без `frequency` разрешён для существующей periodic-серии
  (`>= 2`) и для daily только как `1` (no-op); `daily` + `>= 2` → `400`;
- изменение `frequency` или `interval_days` считается изменением расписания:
  будущие незавершённые экземпляры пересоздаются, прошлые и завершённые
  сохраняются.

`SeriesResponse` получает:

```json
{"frequency": "daily" | "periodic", "interval_days": 1}
```

`daily` остаётся значением по умолчанию для старых клиентов.

## 4. Модель и миграция

### 4.1. Перечисления

`src/backend/task_management/enums.py`:

```python
class SeriesFrequency(StrEnum):
    DAILY = "daily"
    PERIODIC = "periodic"

class TaskRecurrence(StrEnum):
    ONE_OFF = "one_off"
    REGULAR = "regular"
    PERIODIC = "periodic"
```

### 4.2. `TaskSeries`

`src/backend/models/task_series.py`:

```text
interval_days SMALLINT NOT NULL DEFAULT 1
```

Ограничения:

```text
CHECK frequency IN ('daily', 'periodic')
CHECK interval_days >= 1
CHECK (
    (frequency = 'daily' AND interval_days = 1)
    OR
    (frequency = 'periodic' AND interval_days >= 2)
)
```

Имена в ORM-метаданных после naming convention:

- `ck_task_series_frequency`;
- `ck_task_series_interval_days_positive`;
- `ck_task_series_frequency_interval`.

Старое ограничение `ck_task_series_frequency_daily` удаляется.

### 4.3. Миграция

**Отличие от исходного задания:** в репозитории уже существует ревизия
`0004_task_series_generation_idx`, поэтому новая миграция —
`alembic/versions/0005_periodic_task_series.py` с
`down_revision = "0004_task_series_generation_idx"`. Создавать вторую
ревизию с номером `0004` нельзя: это сломает цепочку Alembic.

`upgrade()`:

1. Добавляет `interval_days SMALLINT NOT NULL DEFAULT 1`.
2. Удаляет `ck_task_series_frequency_daily`.
3. Удаляет `ck_task_series_frequency`.
4. Создаёт новые check constraints из раздела 4.2.

`downgrade()`:

1. Нормализует периодические строки в daily, чтобы они удовлетворяли старым
   ограничениям:
   `frequency = 'daily', interval_days = 1`.
2. Удаляет новые constraints.
3. Удаляет `interval_days`.
4. Восстанавливает `ck_task_series_frequency_daily` и
   `ck_task_series_frequency = ('daily')`.

Downgrade намеренно деструктивен для семантики periodic: старые ограничения
не умеют хранить `periodic`, а откат схемы не должен падать на существующих
данных.

## 5. Расписание

Файл `src/backend/task_management/schedule.py`.

### 5.1. `series_due_times`

`daily_due_times` обобщается до:

```python
def series_due_times(
    *,
    starts_on: date,
    ends_on: date | None,
    time_of_day: time,
    timezone: ZoneInfo,
    now: datetime,
    interval_days: int = 1,
    generated_through: datetime | None = None,
    horizon_days: int = HORIZON_DAYS,
) -> list[datetime]:
```

Правила:

- шаг между кандидатами — `interval_days`, а не всегда один день;
- все даты остаются слотами фазы `starts_on + k * interval_days`;
- при возобновлении от `generated_through` берётся первый слот фазы строго
  после локальной даты `generated_through`, а не `generated_date + 1`;
- первый кандидат — первый слот фазы не раньше `max(starts_on,
  local_now.date())`; если его время уже прошло, берётся следующий слот фазы;
- `ends_on` включающий;
- первый слот за горизонтом возвращается один раз только при
  `generated_through is None`; при возобновлении — пустой список;
- `interval_days < 1` → `ValueError`.

### 5.2. `next_occurrence_after_date`

Новый helper для переноса просроченной задачи:

```python
def next_occurrence_after_date(
    *,
    anchor: date,
    interval_days: int,
    time_of_day: time,
    timezone: ZoneInfo,
    now: datetime,
) -> datetime:
```

Правила:

- возвращает первый слот фазы `anchor + k * interval_days`, дата которого
  строго больше локальной даты `now`;
- локальное время `time_of_day` сохраняется, включая DST-разрешение через
  `localize_wall_time`;
- если `anchor` в будущем, возвращается `anchor` (первый слот);
- `interval_days < 1` → `ValueError`.

### 5.3. Использование

- `SeriesService._generate_series` передаёт `series.interval_days`.
- `TaskService` использует `next_occurrence_after_date` при переключении
  регулярности.
- `daily_due_times` больше не используется в production-коде; тесты
  переводятся на `series_due_times`.

## 6. Репозиторий и порты

Файлы:
- `src/backend/task_management/ports.py`;
- `src/backend/task_management/repository.py`;
- `tests/task_management_fakes.py`.

### 6.1. `get_series`

Добавляется параметр:

```python
async def get_series(
    self,
    user_id: int,
    series_id: int,
    *,
    for_update: bool = False,
    include_deleted: bool = False,
) -> TaskSeries | None:
```

- по умолчанию поведение прежнее: soft-deleted серия не находится;
- `include_deleted=True` возвращает серию с `deleted_at != NULL`;
- нужен для `TaskDetail.recurrence` и `_lock_update_target`.

### 6.2. `get_series_by_ids`

Дополнительный batch-метод, необходимый для списка задач:

```python
async def get_series_by_ids(
    self,
    user_id: int,
    series_ids: Sequence[int],
    *,
    include_deleted: bool = False,
) -> dict[int, TaskSeries]:
```

- пустой вход — пустой словарь без запроса;
- фильтрует по `user_id`;
- при `include_deleted=True` не фильтрует `deleted_at`;
- используется в `TaskService._details`, чтобы не делать N+1 запросов при
  вычислении `recurrence` для страницы.

### 6.3. `hard_delete_tasks`

```python
async def hard_delete_tasks(
    self,
    task_ids: Sequence[int],
    user_id: int,
) -> None:
```

- физически удаляет только задачи пользователя;
- `task_labels`, `task_reminders` и `reminder_deliveries` удаляются
  FK cascade;
- пустой вход — no-op;
- не коммитит.

### 6.4. Фильтр `recurrence` в `list_tasks`

- `regular` → join `task_series` по `(series_id, user_id)` и
  `frequency = 'daily'`;
- `periodic` → join `task_series` и `frequency = 'periodic'`;
- `one_off` → `Task.series_id IS NULL`;
- в join нет фильтра `task_series.deleted_at IS NULL`: soft-deleted серия
  должна сохранять исторические задачи в фильтре.

### 6.5. Fake-репозиторий

`FakeTaskManagementRepository` повторяет:

- `get_series(..., include_deleted=False)`;
- `get_series_by_ids(...)`;
- `hard_delete_tasks(...)`, включая удаление связанных
  `task_labels`, `task_reminders`, `reminder_deliveries`;
- `list_tasks` с фильтром по `frequency`, включая soft-deleted серии;
- `make_series(...)` получает `frequency="daily"` и `interval_days=1` по
  умолчанию.

## 7. Сервисы

### 7.1. `SeriesService.create`

Добавляются параметры:

```python
frequency: SeriesFrequency = SeriesFrequency.DAILY,
interval_days: int | None = None,
```

- `daily` нормализуется в `interval_days = 1`; `interval_days` допускается
  только `None`/`1`;
- `periodic` требует `interval_days >= 2`;
- значения сохраняются в `TaskSeries`;
- `_generate_series` использует сохранённый интервал.

### 7.2. `SeriesService.update`

Добавляются параметры:

```python
frequency: SeriesFrequency | None = None,
interval_days: int | None = None,
```

- если поле не передано, берётся текущее значение серии;
- `frequency = daily` нормализует интервал в `1`;
- `frequency = periodic` без нового интервала использует текущий интервал,
  если серия уже periodic; иначе `400`;
- `interval_days` без `frequency` разрешён только для existing periodic;
- изменение `frequency` или `interval_days` включает `schedule_changed`;
- при `schedule_changed` будущие незавершённые экземпляры soft-delete и
  пересоздаются, прошлые и завершённые не меняются;
- шаблонные напоминания при смене frequency/interval не переписываются:
  интервал напоминания остаётся клиентским.

### 7.3. `SeriesService.create_from_task`

```python
async def create_from_task(
    self,
    user_id: int,
    task: Task,
    *,
    frequency: SeriesFrequency,
    interval_days: int | None = None,
) -> TaskSeries:
```

- вызывающий слой гарантирует, что задача не привязана к активной серии;
  `series_id`, указывающий на soft-deleted серию, очищается до изменения
  `due_at`, а затем перезаписывается новой серией (это избегает конфликта
  старого `(series_id, due_at)`);
- валидирует frequency/interval;
- берёт авторскую таймзону из `users.timezone` (fallback `UTC`);
- `starts_on` — локальная дата `task.due_at` в авторской зоне;
- `time_of_day` — локальное время `task.due_at`;
- `ends_on = NULL`;
- снимок: title, description, priority, project_id, labels, reminders,
  `reminders_enabled`;
- шаблонные `scheduled`-напоминания получают `interval_days` серии только
  для `periodic`; для `regular` сохраняется клиентский интервал;
- инстанс-напоминания текущей задачи не пересоздаются;
- проект валидируется как при создании серии (архив → `400`, отсутствие →
  `404`);
- серия и её labels/reminders сохраняются;
- `task.series_id = series.id` до вызова `_generate_series`;
- `_generate_series` не создаёт дубль на `task.due_at`, потому что текущая
  задача уже входит в `list_series_due_times`;
- не коммитит.

### 7.4. `SeriesService.detach_task`

```python
async def detach_task(
    self,
    user_id: int,
    task: Task,
    series: TaskSeries,
) -> None:
```

- серия уже locked вызывающим слоем;
- загружает и блокирует задачи серии через
  `get_series_tasks_for_update`;
- физически удаляет все будущие незавершённые экземпляры, кроме `task.id`
  (`completed_at IS NULL AND due_at > now`);
- отменяет шаблонные напоминания через `cancel_series_reminders`;
- soft-delete серии;
- `task.series_id = NULL`;
- текущая задача и остальная история (завершённые/просроченные) остаются;
- не коммитит.

### 7.5. `TaskService._apply_task_update`

Обновление разовой задачи выносится в приватный метод, чтобы использовать его
до конвертации:

```python
async def _apply_task_update(
    self,
    user_id: int,
    task: Task,
    *,
    title: str | None,
    description: str | None | UnsetType,
    priority: Priority | None,
    due_at: datetime | None,
    project_id: int | None | UnsetType,
    label_ids: Sequence[int] | None,
    reminders_enabled: bool | None,
    reminders: Sequence[ReminderInput] | None,
) -> None:
```

Поведение совпадает с текущей веткой разовой задачи: валидация, обновление
полей, перестройка напоминаний при изменении due/reminders/включении тумблера.

### 7.6. `TaskService.update`

Добавляются параметры:

```python
recurrence: TaskRecurrence | None = None,
interval_days: int | None = None,
```

Ветки:

| Было | Стало | Действие |
| --- | --- | --- |
| one_off | one_off | `_apply_task_update`; при явном `recurrence=one_off` очистить stale `series_id`, указывающий на soft-deleted серию |
| one_off | regular/periodic | очистить stale soft-deleted `series_id`, `_apply_task_update`, при необходимости перенос due, `create_from_task` |
| series | one_off | `detach_task`, затем `_apply_task_update` к текущей задаче и при необходимости перенос due по сохранённой фазе серии |
| series | series | `SeriesService.update` с frequency/interval и остальными полями |

Здесь «series» в первой колонке — только активная серия; soft-deleted серия
попадает в ветку `one_off`.

Перенос просроченной задачи:

```python
async def _shift_overdue_task(
    self,
    user_id: int,
    task: Task,
    *,
    anchor: date,
    interval_days: int,
    timezone: ZoneInfo,
    reminders: Sequence[ReminderInput] | None = None,
) -> None:
```

- no-op, если задача завершена или `due_at > now`;
- `time_of_day` берётся из текущего `task.due_at` в переданной зоне;
- `new_due = next_occurrence_after_date(...)`;
- после смены `due_at` напоминания задачи перестраиваются от нового времени
  по правилам из `reminders`, если они переданы в том же `PATCH`, иначе по
  сохранённым правилам задачи;
- для one_off → series: `anchor` — локальная дата итогового `due_at`,
  `timezone` — авторская зона;
- для series → one_off: `anchor = series.starts_on`,
  `interval_days = series.interval_days`, `timezone = series.timezone`.

Для `series -> one_off` реализация сначала выполняет detach (пока у задачи ещё
есть `series_id`), затем применяет остальные поля и переносит `due_at` по
сохранённым `anchor`/`interval_days`/`timezone`. Иначе перенос просроченной
задачи на слот, где уже есть будущий экземпляр, нарушит уникальный индекс
`tasks(series_id, due_at)`. Транзакция запроса делает операцию атомарной:
ошибка валидации после detach откатывает и удаление, и soft-delete серии.

### 7.7. `TaskService.delete`

- загружает задачу и серию через `_lock_update_target`;
- если серия отсутствует (one-off или soft-deleted) — soft-delete только
  задачи и отмена её напоминаний;
- если серия активна и `task.completed_at IS NULL and task.due_at > now` —
  `SeriesService.delete`;
- иначе (завершённая или просроченная) — soft-delete только задачи и отмена
  её напоминаний.

### 7.8. `_lock_update_target`

- читает задачу без lock;
- если `series_id` задан, загружает серию с `include_deleted=True` и
  `for_update=True`;
- soft-deleted серия возвращается как `None`, чтобы вызывающий слой трактовал
  задачу как разовую;
- отсутствующая серия (нарушение целостности) по-прежнему `404`;
- порядок lock: серия → задача.

### 7.9. `TaskDetail.recurrence`

`TaskDetail` получает поле:

```python
recurrence: TaskRecurrence
```

- `_detail` читает серию с `include_deleted=True`;
- `_details` использует `get_series_by_ids(..., include_deleted=True)`;
- отсутствующая серия при непустом `series_id` трактуется как `one_off`
  (защита от повреждённых данных);
- soft-deleted серия сохраняет `regular`/`periodic`.

## 8. Жизненные циклы и примеры

### 8.1. Разовая задача → regular/periodic

Исходно: `due_at = 2026-11-01 09:00 Europe/Moscow`, `now = 2026-11-04 12:00 UTC`,
`recurrence = periodic`, `interval_days = 3`.

1. Применяются остальные поля.
2. `due_at <= now`, якорь = `2026-11-01`.
3. Первый слот фазы строго после `2026-11-04`: `2026-11-07 09:00`.
4. Серия: `starts_on = 2026-11-07`, `time_of_day = 09:00`,
   `interval_days = 3`.
5. Текущая задача получает `series_id` и `due_at = 2026-11-07 06:00 UTC`.
6. Генератор создаёт `2026-11-10`, `2026-11-13`, ... и не дублирует
   `2026-11-07`.

### 8.2. Series → one_off

Исходно: daily-серия, текущая задача просрочена, есть будущие экземпляры.

1. Применяются остальные поля к текущей задаче.
2. `due_at <= now`, anchor = `series.starts_on`, interval = 1 или N.
3. Текущая задача переносится на завтра (daily) или следующий слот фазы.
4. Будущие незавершённые экземпляры, кроме текущей, физически удаляются.
5. Шаблонные напоминания отменяются.
6. Серия soft-delete, текущая задача `series_id = NULL`.
7. Завершённые и просроченные экземпляры остаются историей.

### 8.3. Повторное включение после выключения

После выключения у задачи `series_id = NULL`, старая серия soft-deleted.
Повторный `PATCH` с `recurrence = regular/periodic` создаёт новую серию; старая
история остаётся привязанной к soft-deleted серии.

Если задача всё ещё ссылается на soft-deleted серию (например, её редактировали
после выключения), повторное включение регулярности создаёт новую серию и
перезаписывает `task.series_id`. Старая soft-deleted серия и её остальные
экземпляры не меняются; текущая задача теряет ссылку на старую историю.

## 9. Принятые решения и допущения

1. **Авторская таймзона** — `users.timezone`; если не задана, используется
   `UTC`. `due_at` переносится в этой зоне, локальное время сохраняется.
2. **Завершённые задачи не переносятся** при переключении регулярности.
3. **Просроченные экземпляры истории** при detach не удаляются и не
   переписываются; физически удаляются только будущие незавершённые, кроме
   текущей.
4. **История soft-deleted серии** сохраняет `series_id`; редактирование и
   удаление такой задачи работает как с разовой.
5. **`regular` в фильтре** означает только `frequency = 'daily'`;
   `periodic` — только `frequency = 'periodic'`.
6. **Шаблонное `scheduled`-напоминание** получает интервал серии только при
   конвертации one_off → periodic. При one_off → regular и при явном series
   API интервал остаётся клиентским. Инстанс-напоминания текущей задачи не
   пересоздаются.
7. **`POST /api/v1/tasks`** по-прежнему создаёт только разовую задачу.
8. **`TaskResponse.recurrence`** вычисляется по серии, включая soft-deleted.
9. **Batch-метод `get_series_by_ids`** добавлен сверх исходного списка, чтобы
   список задач не делал N+1 запрос к сериям.
10. **Миграция — `0005`**, потому что `0004` уже занята
    (`0004_task_series_generation_idx`).
11. **Detach не отменяет instance-напоминания у оставшихся просроченных
    экземпляров истории.** Это буквальное следование требованию: отменяются
    только шаблонные напоминания. Если продукт захочет полностью «заморозить»
    историю, это отдельное решение.

### 9.1. Отличия от исходного задания

- **Номер миграции:** `0005_periodic_task_series`, а не `0004`: ревизия
  `0004_task_series_generation_idx` уже существует.
- **`get_series_by_ids`:** добавлен batch-метод, чтобы список задач не делал
  N+1 запрос при вычислении `recurrence`.
- **Явный `recurrence=one_off` у задачи со soft-deleted серией:** очищает
  `series_id`, чтобы ответ соответствовал запросу. Обычное редактирование без
  этого поля сохраняет историческую ссылку.
- **`PATCH /task-series` с `interval_days=1` для daily:** разрешён как no-op;
  `daily` + `>= 2` по-прежнему `400`.
- **Detach и напоминания истории:** отменяются только шаблонные напоминания;
  instance-напоминания просроченных/завершённых экземпляров не трогаются.

## 10. Не-цели

- Недельная, месячная и годовая регулярность.
- Отдельные исключения для одного экземпляра серии.
- UI/Frontend.
- Новые runtime/dev-зависимости.
- Физическое удаление истории (кроме detach будущих незавершённых экземпляров
  и `hard_delete_tasks` внутри него).
- Восстановление soft-deleted серий.

## 11. Тестовая стратегия

| Файл | Что проверяет |
| --- | --- |
| `tests/test_task_management_models.py` | `interval_days`, check constraints, отсутствие `frequency_daily` |
| `tests/test_task_management_migration.py` | цепочка `0003 -> 0004 -> 0005`, upgrade/downgrade-операции |
| `tests/test_task_management_schedule.py` | periodic N=3, фаза, `generated_through`, `ends_on`, horizon, DST, `next_occurrence_after_date` |
| `tests/test_task_management_repository.py` | SQL join для `regular`/`periodic`, `one_off`, `include_deleted`, `get_series_by_ids`, `hard_delete_tasks` |
| `tests/test_task_management_series.py` | create/update periodic, schedule_changed, create_from_task, detach_task |
| `tests/test_task_management_tasks.py` | переключения future/overdue/completed, перенос due, hard-delete будущих, сохранение истории, archived project, валидация interval |
| `tests/test_task_management_repository.py` + `tests/task_management_fakes.py` | паритет fake-репозитория |
| `tests/test_tasks_api.py` | `recurrence` в response/filter/fingerprint, валидация PATCH, DELETE-делегирование |
| `tests/test_task_series_api.py` | periodic create/update/response, validation, backward compatibility |

## 12. Документация

- `README.md`: таблица endpoint'ов, `recurrence`/`interval_days`, правила
  переключения и DELETE, periodic-генерация.
- `docs/superpowers/specs/2026-11-03-task-management-database-design.md`:
  точечно обновить цели, не-цели, решения, схему `task_series`, значения
  `frequency`, жизненные циклы 9.3/9.5/9.7, раздел DST и влияние на API.
- Новый документ (этот файл) фиксирует решения по toggle.
- План: `docs/superpowers/plans/2026-11-04-task-recurrence-toggle.md`.

## 13. Проверка

После реализации:

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
```
