# Производительность, cursor-пагинация и защита от дорогих запросов

- Дата: 2026-11-04
- Статус: согласовано к реализации
- Область: `backend/` (FastAPI, Python 3.14, PostgreSQL, SQLAlchemy 2.x)
- Потребитель контракта: Telegram Mini App клиент

## 1. Зачем это нужно

Ревью выявило несколько реальных проблем, которые проявляются на
пользовательских объёмах данных:

1. `GET /api/v1/tasks` возвращает весь список без лимита, а на каждый
   task делает ещё три запроса (labels, reminders, project). 200 задач —
   это 600+ round trips и неограниченный ответ.
2. `GET /api/v1/projects` и любое чтение/изменение одного проекта
   перечисляют все проекты пользователя, а прогресс и цвет считаются
   отдельными запросами на каждый проект.
3. Генерация recurring-series делает flush на каждый task, label и
   reminder, а периодический job каждые 15 минут заново выбирает уже
   догнанные и завершённые серии.
4. `list_series_due_times` возвращает всю историю серии, хотя для
   генерации нужны только будущие слоты.
5. Unarchive проекта делает отдельный запрос reminder'ов на каждую
   серию и задачу.
6. `starts_on`/`due_at` могут быть далёким прошлым; `daily_due_times` и
   `first_scheduled_time` идут по дням от этой даты до `now`, тратя
   секунды CPU в event loop.
7. `label_ids` и `description` не ограничены; для серий каждый label
   валидируется отдельным SELECT под блокировкой строки.
8. Reminder dispatch не ограничен по количеству и времени: backlog или
   зависший Telegram могут превысить `NATS_ACK_WAIT`.
9. У БД нет `statement_timeout` и `pool_timeout`.
10. Критичные пути защиты почти не проверяются тестами: route-тесты
    подменяют auth-зависимость, cross-user body IDs не проверяются,
    невалидные/просроченные токены не доходят до HTTP-слоя.

## 2. Цели

1. Перевести списки tasks и projects на cursor-based pagination с
   фиксированным порядком и opaque-курсором.
2. Убрать N+1 при выдаче задач и проектов: batch-загрузка связанных
   данных и агрегатов.
3. Сделать генерацию серий предсказуемой: выбор только реально
   нужных серий, bulk-вставка, индекс под выборку.
4. Ограничить `list_series_due_times` будущими слотами.
5. Убрать N+1 при unarchive проекта.
6. Валидировать пользовательские даты диапазоном
   `2025-01-01 .. 2100-12-31` и убрать пошаговые циклы по дням.
7. Ограничить `description` и `label_ids`; batch-валидация labels для
   серий.
8. Ограничить reminder dispatch: не более 100 доставок и 240 секунд на
   запуск, последовательная отправка.
9. Добавить `statement_timeout=30s` и `pool_timeout=10s` (настраиваемые).
10. Закрыть критические пробелы тестов и добавить измерение покрытия
    через `pytest-cov` без жёсткого порога.

## 3. Не-цели

- Не трогать dev-инфраструктуру: публикацию портов, `.env`,
  compose-пароли, TLS, CORS/ALLOWED_HOSTS, admin-панель, rate limiter,
  auth-сессии и error envelope.
- Не менять контракты авторизации, health, labels, series list,
  color-palette и остальных endpoint'ов.
- Не добавлять cursor-пагинацию в series list или labels list.
- Не добавлять полнотекстовый поиск/trigram-индексы для `q`.
- Не добавлять новые runtime-зависимости. Единственная новая
  зависимость — dev-only `pytest-cov`.
- Не менять формат успешных ответов tasks/projects, кроме добавления
  `next_cursor`; список `items` остаётся.
- Не делать идеальную семантику exactly-once для Telegram: доставка
  остаётся at-least-once.
- Не гарантировать snapshot-консистентность пагинации при конкурентных
  изменениях: курсор стабилен только для неизменного набора данных.
  Если task переносят по `due_at` или проект архивируют между
  страницами, строка может быть пропущена или повторена. Это
  стандартная семантика cursor-пагинации; snapshot/as-of токен не
  добавляем.

## 4. Критерии неудачи

- `GET /tasks` или `GET /projects` без параметров всё ещё возвращает
  неограниченный список.
- Cursor принимает повреждённое значение, чужого пользователя или
  курсор от других фильтров и молча отдаёт неверную страницу.
- Между страницами появляются дубликаты или пропуски.
- Даты вне `2025-01-01 .. 2100-12-31` принимаются API или сервисом.
- `daily_due_times`/`first_scheduled_time` снова идут по дням от
  древней даты.
- Генерация серии делает flush на каждую строку или каждые 15 минут
  захватывает уже догнанную/завершённую серию.
- Unarchive снова делает запрос на каждую серию/задачу.
- Reminder dispatch может обработать больше 100 доставок или не
  останавливаться по deadline 240 секунд (с учётом одной in-flight
  отправки).
- `statement_timeout`/`pool_timeout` не попадают в engine.
- Тесты, ruff, format или mypy падают; команда coverage отсутствует.
- Любой затронутый endpoint перестаёт требовать Bearer-токен.

## 5. Принятые решения

1. Пагинация ломающая: ответ `{items, next_cursor}`, `limit` по
   умолчанию 50, максимум 100. Старый клиент без `limit` получает
   первую страницу.
2. Порядок tasks: `(due_at ASC, id ASC)`. Порядок projects:
   `(is_archived ASC, lower(name) ASC, id ASC)`, где `is_archived=0`
   для активных и `1` для архивных.
3. Курсор — opaque `base64url(JSON)` без padding, версия 1, содержит
   user id, fingerprint фильтров и ключ сортировки. Максимум 512
   символов.
4. Fingerprint фильтров обязателен: менять фильтры при переходе на
   следующую страницу нельзя.
5. Границы дат: `2025-01-01T00:00:00Z .. 2100-12-31T23:59:59.999999Z`
   включительно. Для `date`-полей — `2025-01-01 .. 2100-12-31`.
6. `description` — максимум 5000 символов. `label_ids` — максимум 100
   элементов. Дубликаты удаляются до валидации.
7. DB timeouts: `DB_STATEMENT_TIMEOUT_MS=30000`,
   `DB_POOL_TIMEOUT_SECONDS=10`, оба настраиваются через env.
8. Reminder dispatch: batch 100, soft deadline 240 секунд (проверка
   перед каждой следующей доставкой), отправка последовательная.
   Худший случай — 240 с + одна отправка (до 10 с) + накладные
   расходы БД; это остаётся ниже `NATS_ACK_WAIT=300`.
9. Генерация серий: горизонт остаётся 90 дней; серия выбирается, если
   `generated_through` пуст или меньше `now + 90d - 1d`, и `ends_on`
   не старше вчерашней UTC-даты. Завершённые серии не выбираются.
10. Bulk-запись серии: один flush на tasks, один на task_labels, один
    на reminders (максимум 3 flush вместо `O(n)`).
11. `list_series_due_times` получает `now` и возвращает только
    `due_at >= now`.
12. Unarchive: ровно 4 запроса (series, series reminders, tasks, task
    reminders) независимо от количества сущностей.
13. Coverage: `pytest-cov` как dev-зависимость, команда
    `pytest --cov=backend --cov-report=term-missing`, без
    `--cov-fail-under`.

## 6. Контракты

### 6.1. Cursor-пагинация tasks

`GET /api/v1/tasks`

Новые query-параметры:

- `limit: int = 50`, допустимо `1..100`.
- `cursor: str | None`, максимум 512 символов.

Существующие фильтры (`date`, `project_ids`, `priorities`, `status`,
`recurrence`, `q`, `include_archived_projects`) сохраняются.

Ответ:

```json
{
  "items": [],
  "next_cursor": "eyJ2IjoxLCJ1IjoxLCJmIjoi...",
}
```

`next_cursor` равен `null`, когда следующей страницы нет. Клиент
передаёт его в `cursor` без изменения остальных фильтров.

Курсор:

```json
{"v": 1, "u": 1, "f": "<sha256 hex>", "k": ["2026-11-04T09:00:00+00:00", 42]}
```

- `v` — версия схемы.
- `u` — `user_id`; курсор другого пользователя отклоняется.
- `f` — fingerprint канонических фильтров.
- `k` — `[due_at ISO-8601, task_id]`.

Fingerprint считается по каноническому JSON:

```json
{
  "date": "2026-11-04" | null,
  "include_archived_projects": false,
  "priorities": ["high"],
  "project_ids": [1, 2],
  "q": "текст" | null,
  "recurrence": "all",
  "status": "all"
}
```

Списки `project_ids` и `priorities` сортируются перед сериализацией.
`q` не нормализуется (важно сохранить точное совпадение фильтра).

Запрос к repository делает `LIMIT limit + 1`; сервис отдаёт первые
`limit` строк, а `next_cursor` строит по последней отданной строке,
только если была лишняя.

Гарантия «без дубликатов и пропусков» действует только при неизменном
наборе данных между запросами. Конкурентное изменение `due_at`
(и `archived_at`/`name` у проектов) может переместить строку через
границу курсора; snapshot-токен в этой итерации не вводится.

### 6.2. Cursor-пагинация projects

`GET /api/v1/projects`

Новые query-параметры: `limit` и `cursor` с теми же правилами.
`include_archived` сохраняется.

Ответ:

```json
{"items": [], "next_cursor": null}
```

Курсор:

```json
{"v": 1, "u": 1, "f": "<sha256 hex>", "k": [0, "alpha", 42]}
```

- `k[0]` — `0` для активного, `1` для архивного проекта.
- `k[1]` — `lower(name)`.
- `k[2]` — `project_id`.

Fingerprint: `{"include_archived": false}`.

Запрос к repository сортирует по
`(case(archived_at is null -> 0 else 1), lower(name), id)` и фильтрует
по курсорному ключу. `LIMIT limit + 1`, как у tasks.

### 6.3. Ошибки курсора

Любой из случаев ниже — `400 bad_request` с описанием
`"Invalid pagination cursor"`:

- невалидный base64/JSON;
- неизвестная версия;
- неверный user id;
- несовпавший fingerprint фильтров;
- неверное количество/типы элементов `k`;
- длина больше 512 символов.

Тело ошибки — обычный плоский конверт с `request_id`.

### 6.4. Валидация дат

Общий модуль `backend.task_management.date_bounds`:

```python
MIN_SUPPORTED_DATE = date(2025, 1, 1)
MAX_SUPPORTED_DATE = date(2100, 12, 31)
MIN_SUPPORTED_DATETIME = datetime(2025, 1, 1, tzinfo=UTC)
MAX_SUPPORTED_DATETIME = datetime(2100, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
```

Функции `require_supported_date(value)` и
`require_supported_datetime(value)` возвращают значение или поднимают
`ValueError`.

Применяется к:

- `TaskCreate.due_at`, `TaskUpdate.due_at`;
- `TaskFiltersQuery.date`;
- `SeriesCreate.starts_on`, `SeriesCreate.ends_on`;
- `SeriesUpdate.starts_on`, `SeriesUpdate.ends_on`;
- сервисным `TaskService.create/update` и
  `SeriesService.create/update`.

На API-слое нарушение диапазона — `422 validation_error`. При прямом
вызове сервиса (не через Pydantic) — `BadRequestError` с кодом
`bad_request`. Проверка `ends_on < starts_on` остаётся
`BadRequestError`.

`daily_due_times` и `first_scheduled_time` перестают идти по дням от
`starts_on`/`due_at`: они вычисляют ближайшую локальную дату к `now`
арифметикой и делают не более пары итераций на DST-коррекцию. Цикл по
горизонту остаётся ограниченным 90 днями.

`next_reminder_slot` также перестаёт линейно шагать по интервалам:
количество шагов вычисляется как
`max(0, (local_now.date() - local_current.date()).days // interval_days)`,
после чего остаётся не более 1–2 итераций на DST-границу. Это касается
scheduled-напоминаний, которые долго были приостановлены или отстали.

### 6.5. Batch-выдача tasks

Новые методы repository:

```python
async def list_task_labels_for_tasks(
    self, user_id: int, task_ids: Sequence[int]
) -> dict[int, Sequence[Label]]: ...

async def list_task_reminders_for_tasks(
    self, user_id: int, task_ids: Sequence[int]
) -> dict[int, Sequence[TaskReminder]]: ...

async def get_projects_by_ids(
    self, user_id: int, project_ids: Sequence[int]
) -> dict[int, Project]: ...
```

`TaskService.list` после получения страницы собирает уникальные
`task_ids` и `project_ids`, делает не более трёх batch-запросов и
собирает `TaskDetail` в памяти. `TaskService.get` продолжает
использовать одиночный `_detail`.

### 6.6. Batch-выдача projects

Новые методы:

```python
async def project_progress_for_projects(
    self, user_id: int, project_ids: Sequence[int]
) -> dict[int, tuple[int, int]]: ...

async def get_palette_colors_by_ids(
    self, color_ids: Sequence[int]
) -> dict[int, ColorPalette]: ...
```

`PaletteService.get_colors(color_ids) -> dict[int, ColorPalette]`
проксирует batch-запрос и поднимает `NotFoundError`, если хотя бы один
цвет отсутствует.

`ProjectService.list` делает 1 запрос проектов, 1 групповой запрос
прогресса и 1 запрос цветов. `ProjectService.get_summary` делает 3
запроса: проект, прогресс, цвет.

`api/routes/projects.py` для одиночного проекта вызывает
`get_summary`, а не `list(include_archived=True)`.

### 6.7. Генерация серий

`list_series_for_generation(now)`:

- `deleted_at IS NULL`;
- проект активен и не удалён;
- `generated_through IS NULL OR generated_through < now + 90d - 1d`;
- `ends_on IS NULL OR ends_on >= (now в UTC).date() - 1 день`.

`_generate_series` собирает списки `Task`, `TaskLabel`,
`TaskReminder` и вызывает:

```python
async def add_tasks(self, tasks: Sequence[Task]) -> Sequence[Task]: ...
async def add_task_label_rows(self, rows: Sequence[TaskLabel]) -> None: ...
async def add_reminders(self, reminders: Sequence[TaskReminder]) -> None: ...
```

Каждый метод делает `add_all` и один `flush`. `add_task`,
`add_task_labels`, `add_reminder` остаются для одиночных сценариев.

`list_series_due_times(series_id, user_id, *, now)` фильтрует
`Task.due_at >= now`.

Новый partial index:

```python
Index(
    "ix_task_series_generated_through_active",
    "generated_through",
    postgresql_where=text("deleted_at IS NULL"),
)
```

Поведение генерации не меняется: горизонт 90 дней, без backfill,
идемпотентность по `(series_id, due_at)`, `FOR UPDATE SKIP LOCKED`.

### 6.8. Unarchive

`resume_project_reminders` вместо запроса на каждую серию/задачу:

1. Загружает серии проекта.
2. Одним `IN`-запросом загружает reminders этих серий.
3. Загружает незавершённые задачи проекта.
4. Одним `IN`-запросом загружает reminders этих задач.

Собирает `dict[series_id, list[TaskReminder]]` и
`dict[task_id, list[TaskReminder]]` и применяет те же правила
`next_run_at`/`timezone`, что и сейчас.

### 6.9. Reminder dispatch

Константы в `task_management/reminders.py`:

```python
_DISPATCH_BATCH_SIZE = 100
_DISPATCH_TIMEOUT_SECONDS = 240.0
```

`list_due_reminders(now, *, limit)` добавляет `LIMIT`.

`ReminderDispatcher` получает опциональный `monotonic` clock (для
тестов). В начале каждой итерации:

```python
if monotonic() - started >= _DISPATCH_TIMEOUT_SECONDS:
    break
```

Это soft budget, а не жёсткая отмена in-flight отправки: уже начатая
доставка завершается (Telegram timeout 10 с), после чего цикл
останавливается. Необработанные due reminders остаются на следующий
запуск. Порядок, lease, backoff, идемпотентность и at-least-once
семантика не меняются.

### 6.10. DB timeouts

`Settings`:

```python
db_statement_timeout_ms: int = Field(default=30_000, gt=0)
db_pool_timeout_seconds: int = Field(default=10, gt=0)
```

`Database.__init__`:

```python
create_async_engine(
    settings.database_url,
    ...,
    pool_timeout=settings.db_pool_timeout_seconds,
    connect_args={
        ...,
        "command_timeout": settings.db_statement_timeout_ms / 1000,
        "server_settings": {
            "application_name": settings.app_name,
            "statement_timeout": str(settings.db_statement_timeout_ms),
        },
    },
)
```

`command_timeout` — клиентская страховка asyncpg; `statement_timeout` —
серверный лимит. Если PgBouncer не пропускает startup-параметр,
`command_timeout` продолжает ограничивать команду.

`.env.example` и `compose.yml` получают
`DB_STATEMENT_TIMEOUT_MS` и `DB_POOL_TIMEOUT_SECONDS` с дефолтами
`30000` и `10`.

### 6.11. Input limits

- `description` в `TaskCreate`, `TaskUpdate`, `SeriesCreate`,
  `SeriesUpdate`, `ProjectCreate`, `ProjectUpdate` — `max_length=5000`.
- `label_ids` в `TaskCreate`, `TaskUpdate`, `SeriesCreate`,
  `SeriesUpdate` — `max_length=100`.
- `SeriesService._validate_labels` сначала удаляет дубликаты, затем
  одним запросом `get_labels_by_ids` проверяет владение и количество.
- Нарушение — `422 validation_error`.

### 6.12. Тесты и покрытие

Добавляется dev-зависимость `pytest-cov` и команда:

```bash
poetry run pytest --cov=backend --cov-report=term-missing
```

Без `--cov-fail-under`.

Новые критические тесты:

1. Parameterized-матрица: каждый непубличный API-роут без
   `Authorization` возвращает `401`.
2. Cross-user body IDs: чужой `project_id`, `label_ids`, `series_id`
   в create/update, complete/uncomplete, archive/unarchive — 404 и
   отсутствие мутаций.
3. HTTP-уровень: malformed/expired/wrong-secret bearer для
   `/auth/me` и `/api/v1/tasks` — 401 и корректный конверт.
4. Origin для `/auth/refresh` и `/auth/logout`: запрещённый origin —
   403; отсутствующий origin разрешён.
5. JWT hardening: отсутствующие обязательные claims, неверные
   `sid`/`jti`, `alg=none`, `alg=RS256` — 401.
6. SQL-compile тесты `SqlAlchemyAuthRepository` для
   `get_session_for_update` (`FOR UPDATE` + hash) и
   `revoke_user_sessions`/`revoke_family` (scope).
7. SQL-compile тесты tenant-scoped repository-методов tasks/series.
8. Для новых batch-методов — проверка, что запрос содержит `IN` и
   `user_id`, а не запрос на каждую сущность.

## 7. Изменения по слоям

- `api/`: `pagination.py` (новый), `task_schemas.py`, `routes/tasks.py`,
  `routes/projects.py`.
- `task_management/`: `date_bounds.py` (новый), `tasks.py`,
  `projects.py`, `series.py`, `schedule.py`, `reminders.py`,
  `repository.py`, `ports.py`, `palette.py`, `labels.py`.
- `core/`: `config.py`.
- `db/`: `session.py`.
- `models/`: `task_series.py`.
- `alembic/`: миграция `0004_task_series_generation_idx`.
- `tests/`: новые файлы и правки существующих.
- Документация: `README.md` (пагинация, DB env, coverage),
  `.env.example`, `compose.yml`.

## 8. Тестовая стратегия

- TDD: на каждую задачу сначала падающий тест, затем минимальная
  реализация.
- API-тесты используют существующие fake-сервисы; для пагинации fake
  repository сортирует и режет данные по курсору.
- SQL-тесты компилируют запросы и проверяют `WHERE`, `LIMIT`, `IN`,
  `FOR UPDATE`, `ORDER BY`.
- Тесты не требуют PostgreSQL, NATS или Redis.
- После каждого этапа: `pytest -q`, `ruff check`, `ruff format
  --check`, `mypy`.
- В конце: полный прогон и `graphify update .`.

## 9. Миграции и совместимость

- Пагинация ломает контракт `GET /tasks` и `GET /projects`: старый
  клиент без `limit` получает 50 элементов. Это осознанное решение;
  клиент обновляется вместе с backend.
- Курсор привязан к фильтрам; клиент не должен менять фильтры между
  страницами.
- Даты до 2025 года больше не принимаются на вход, но существующие
  строки в БД не переписываются и остаются доступными в списке.
- `description` длиннее 5000 символов остаётся в БД, но повторное
  сохранение такого значения вернёт 422.
- Новая миграция только добавляет partial index; данные не меняются.
- При откате миграции индекс удаляется, код продолжает работать.

## 10. Проверка результата

1. `poetry run pytest -q` — все тесты зелёные.
2. `poetry run ruff check .` — без ошибок.
3. `poetry run ruff format --check src tests` — без изменений.
4. `poetry run mypy src tests` — без ошибок.
5. `poetry run pytest --cov=backend --cov-report=term-missing` —
   покрытие измеряется, критичные новые ветки покрыты.
6. `graphify update .`.
