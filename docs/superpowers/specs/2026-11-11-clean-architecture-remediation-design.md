# Пост-рефакторинг чистой архитектуры: устранение SRP/KISS-долга

- Дата: 2026-11-11
- Статус: на согласовании
- Область: `backend/` (Python 3.14, FastAPI, SQLAlchemy 2.x, PostgreSQL, Taskiq)
- Потребитель контракта: Telegram Mini App / frontend
- Основание: архитектурное ревью от 2026-11-11 по итогам
  `2026-11-10-clean-architecture-refactor-design.md`
- Связанные документы:
  - `docs/superpowers/specs/2026-11-10-clean-architecture-refactor-design.md`
  - `docs/superpowers/specs/2026-11-02-api-error-standardization-design.md`
  - `AGENTS.md`
  - `README.md`

## 1. Зачем это нужно

Базовый рефакторинг на чистую архитектуру завершён: `domain` и
`application` не импортируют FastAPI/SQLAlchemy/ORM, API не импортирует
ORM-модели, composition root существует, HTTP-контракт заморожен
контрактными тестами. Остались нарушения SRP и KISS, которые уже
затрудняют сопровождение:

1. **Мёртвый compatibility-фасад.** `infrastructure/persistence/sqlalchemy/legacy.py`
   содержит 1881 строку делегирующих методов, `application/ports/legacy.py` —
   80-методный composite-протокол. В production они не используются; ссылки
   остались только в `backend/task_management/{ports,repository}.py` и
   `tests/test_composition.py`. Это второй контракт, который нужно
   синхронизировать при каждом изменении портов.
2. **God-сервисы.** `TaskService` — 26 методов, ~1450 строк; `SeriesService` —
   23 метода, ~1260 строк. В `TaskService` смешаны чтение, создание,
   завершение, recurrence-конверсия, пересборка напоминаний и валидация
   ссылок. В `SeriesService` смешаны request-flow и scheduled generation с
   собственными `commit/rollback`. Это разные акторы и разные транзакционные
   границы.
3. **Дублирование в application.** `_validate_project` и `_validate_labels`
   почти одинаковы в `TaskService` и `SeriesService`; `_user_timezone` /
   `_timezone` дублируют резолвинг IANA-зоны и маппинг ошибки.
4. **Презентация в domain.** `domain/task_management/policies.py:517-716`
   содержит `build_reminder_text`, русские тексты, эмодзи, HTML-escaping и
   форматирование дат. Это presentation-правило, а не бизнес-инвариант.
5. **Домен зависит от прикладной ошибки.** `domain/task_management/policies.py`
   поднимает `BadRequestError` из `core.errors` (12 мест). Домен не должен
   знать о request-level семантике.
6. **`core.errors` знает HTTP.** `ErrorSpec` содержит `status_code`
   (`core/errors.py:33-39`), хотя `AGENTS.md` фиксирует обратное. Статус —
   транспортная политика, его место в `api/errors`.
7. **Проверка токена вне composition root.** `api/deps.py:26,255` напрямую
   импортирует `decode_access_token` из `infrastructure`. Порт `TokenIssuer`
   покрывает только выпуск токена, проверка осталась вне порта.
8. **Политика reminders в адаптере.** `ReminderRepository.resume_project_reminders`
   (`reminder_repository.py:393-490`) загружает сущности, применяет доменную
   политику `rebuild_reminder_slot`, мутирует и flush'ит. Adapter содержит
   orchestration/бизнес-правило.
9. **Дублирование и пересборка composition-графа.** `build_series_service` и
   `build_reminder_dispatcher` повторяют сборку адаптеров; API-зависимости
   `get_project_service`/`get_label_service`/`get_task_service` каждая
   вызывают `build_task_management_services`, создавая отдельный граф из
   7 адаптеров и 5 сервисов.
10. **Мелкие adapter/API-дефекты.** `api/routes/labels.py:63-66` резолвит
    цвета по одному вместо batch; `api/routes/projects.py:45-65` содержит
    passthrough-обёртку без ценности.

## 2. Цели

1. Удалить мёртвый composite-фасад (`legacy.py`, `application/ports/legacy.py`,
   `backend/task_management/{ports,repository}.py`) и зависящие от него
   проверки, не удаляя остальные тонкие compatibility-шимы.
2. Разделить `TaskService` на чтение/жизненный цикл и mutation/recurrence
   use cases с общими collaborators.
3. Выделить `SeriesGenerationService` и `SeriesMaterializer`; убрать
   scheduled-generation из request-scoped `SeriesService`, сохранив
   flush-before-dedupe и per-series commit/rollback.
4. Вынести общие `_validate_project`/`_validate_labels`/timezone-резолвинг в
   application-collaborator без изменения запросов и ошибок.
5. Перенести рендеринг текста напоминания из `domain` в application-presenter.
6. Ввести доменные ошибки напоминаний; убрать импорт `core.errors` из
   `domain`.
7. Перенести HTTP-статусы из `core.errors` в `api/errors`, сохранив
   `ErrorCode`/`ErrorSpec` и весь wire-контракт ошибок.
8. Ввести порт `TokenVerifier` и composition-фабрику; убрать прямой импорт
   инфраструктурного декодера из API.
9. Перенести `resume_project_reminders` policy-логику из SQLAlchemy-адаптера
   в application-сервис, оставив адаптеру только чтение/запись.
10. Собрать task-management граф один раз на запрос и один раз на job;
    устранить дублирование wiring.
11. Выполнить два малых API-улучшения: batch-резолв палитры в списке меток и
    удаление passthrough-обёртки project summary.
12. Сохранить без изменений HTTP-контракты, схему БД, бизнес-правила,
    транзакционные границы и производительность.

## 3. Не-цели

- Не менять URL, HTTP-методы, статусы, JSON-формы, error envelope,
  `request_id`, cursor-формат, OpenAPI operation ID и документированные
  ошибки.
- Не менять схему БД и не добавлять Alembic-миграции.
- Не менять бизнес-правила: recurrence, reminder slots, пагинацию,
  soft/hard delete, archive/unarchive, tenant isolation, лимиты, тексты
  ошибок.
- Не менять транзакционные границы: request commit в `Database.session()`,
  per-series commit/rollback в generation, dispatcher commit points,
  auth commit-on-error, series-before-task lock order, flush-before-dedupe.
- Не добавлять runtime/dev-зависимости.
- Не удалять остальные compatibility-шимы (`backend/task_management/*` кроме
  `ports`/`repository`, `backend/auth/*`, `backend/tasks/telegram.py`).
- Не переводить update-роуты на типизированные command-DTO
  (`dict[str, Any]` остаётся): это отдельное низкоприоритетное улучшение,
  требующее массового изменения тестов и не влияющее на поведение.
- Не разбивать `ReminderDispatcher.dispatch_due` и `AuthService.refresh`:
  они длинные, но их transaction/log order критичен; вынос допускается
  отдельным спекулятивным рефакторингом с расширенными transaction-тестами.
- Не дробить `tests/task_management_fakes.py` на отдельные fakes: тестовый
  double может структурно реализовывать несколько портов.
- Не менять admin, кроме использования новых фабрик, если это потребуется
  сигнатурой.
- Не оптимизировать запросы вне перечисленных пунктов.

## 4. Критерии неудачи

Считаем изменение неудачным, если:

- изменился хотя бы один HTTP-контракт: путь, метод, статус, JSON-поле,
  `code`/`description`, cursor, operation ID или документированный набор
  ошибок;
- изменилась схема БД или появилась миграция;
- изменилось поведение recurrence, reminder slots, archive/unarchive,
  soft/hard delete, tenant isolation или тексты ошибок;
- изменился порядок commit/rollback или lock order;
- `domain` импортирует `backend.core.errors`, FastAPI, SQLAlchemy, Pydantic,
  Taskiq, ORM или внешние слои;
- `api` импортирует `backend.infrastructure` или ORM-модели;
- `application` импортирует concrete-адаптеры или `composition`;
- после split сервисов появилось дублирование бизнес-правила или два
  независимых источника истины для reminder slots;
- `TaskService`/`SeriesService` сохраняют прежний объём ответственности под
  новыми именами;
- удалённые compatibility-модули всё ещё импортируются из `src/`;
- composition создаёт больше одного task-management графа на запрос;
- падают `pytest`, `ruff`, `ruff format --check`, `mypy`;
- `graphify update .` не выполнен после изменений.

## 5. Принятые решения

1. **Strangler-подход.** Каждая задача — отдельный commit с зелёным полным
   прогоном. Сначала добавляются новые модули/тесты, затем переключаются
   composition/API, затем удаляется старый код.
2. **Границы поведения заморожены.** Все существующие contract-, OpenAPI-,
   route-auth-, error- и transaction-тесты остаются; допускается только
   обновление импортов и точек сборки.
3. **Legacy cleanup частичный.** Удаляются только мёртвые composite-артефакты.
   Тонкие re-export-шимы остаются для обратной совместимости импортов и
   тестов. Это осознанный компромисс: цена поддержки шимов низкая, цена
   второго 80-методного контракта — высокая.
4. **Split по акторам, не по каждой функции.** `TaskService` делится на
   чтение/жизненный цикл (`TaskService`) и mutation/recurrence
   (`TaskMutationService`). `SeriesService` делится на request-flow
   (`SeriesService`) и scheduled generation (`SeriesGenerationService`).
   Общая материализация одной серии выносится в `SeriesMaterializer`.
5. **Общие collaborators вместо копирования.** `TaskReferences` владеет
   project/label validation и резолвингом IANA-зоны; `TaskDetails` собирает
   `TaskDetail`/списки; `task_inputs.py` содержит чистые проверки title,
   due_at и recurrence/interval. Те же helpers используют task- и
   series-сервисы.
6. **Domain остаётся чистым.** Текст напоминания — application-presenter.
   Доменные ошибки напоминаний — отдельные исключения в
   `domain/task_management/errors.py`; маппинг в `BadRequestError` выполняет
   application.
7. **`core.errors` — транспортно-независимый реестр.** `ErrorCode`,
   `ErrorSpec` (log level, message, description) остаются; `status_code`
   переносится в `api/errors/status.py`. Это приводит код в соответствие с
   `AGENTS.md` без изменения wire-контракта.
8. **`TokenVerifier` — порт.** `JwtTokenVerifier` реализует его в
   infrastructure; composition создаёт verifier и кладёт в `app.state`.
   `decode_access_token` остаётся для совместимости, но API его не импортирует.
9. **Resume reminders — application policy.** `ReminderRepository` получает
   примитив чтения `list_project_reminder_contexts`; application-сервис
   `ProjectReminderService` вычисляет слоты через доменную политику и
   сохраняет их. `pause_project_reminders` остаётся bulk-SQL в адаптере, так
   как не содержит доменных вычислений.
10. **Один граф на запрос.** `composition` получает общий
    `_build_task_management_adapters(session)`. API-зависимости берут сервисы
    из одного `TaskManagementServices`, собранного один раз на request.
11. **Никаких новых публичных API.** Новые классы/функции внутренние;
    существующие имена сервисов, используемые тестами, сохраняются там, где
    это не мешает SRP. Перенесённые методы получают новые владельцы, а
    старые тесты обновляются на новые точки входа.
12. **Докстринги обязательны.** Каждый новый/изменённый класс и функция
    получает английский докстринг по шаблону `AGENTS.md`; устаревшие
    докстринги обновляются в том же изменении.

## 6. Целевая структура

```text
src/backend/
  domain/
    task_management/
      errors.py                  # ReminderConfigurationError
      models.py                  # + ProjectReminderContext
      policies.py                # только бизнес-политики, без текста и core.errors
  application/
    ports/
      auth.py                    # + TokenVerifier
      collaborators.py           # + ProjectReminderCommands
      task_management.py         # - resume_project_reminders; + list_project_reminder_contexts
    task_management/
      task_details.py            # TaskDetails
      task_inputs.py             # чистые проверки title/due_at/recurrence
      task_mutations.py          # TaskMutationService
      task_references.py         # TaskReferences
      series_generation.py       # SeriesGenerationService
      series_materializer.py     # SeriesMaterializer
      series_reminders.py        # build_instance_reminder для materializer и update
      project_reminders.py       # ProjectReminderService
      reminder_presenter.py      # build_reminder_text и форматирование
      reminders.py               # ReminderDispatcher без текста
      tasks.py                   # TaskService: list/get/today/create/complete
      series.py                  # SeriesService: request-flow
  infrastructure/
    auth/tokens.py               # + JwtTokenVerifier
    persistence/sqlalchemy/
      reminder_repository.py     # - resume policy; + primitive context reader
      legacy.py                  # удаляется
  composition/
    auth.py                      # + build_token_verifier
    task_management.py           # единый adapter builder + новые фабрики
  api/
    errors/
      status.py                  # ERROR_STATUS_CODES
      mapping.py                 # использует status.py
      openapi.py                 # использует status.py
    deps.py                      # TaskMutationServiceDep, единый граф, verifier
    routes/
      tasks.py                   # update/uncomplete/delete -> TaskMutationService
      labels.py                  # batch palette
      projects.py                # без _load_summary
  task_management/
    ports.py                     # удаляется
    repository.py                # удаляется
```

## 7. Контракты слоёв

### 7.1. Удаление legacy-фасада

Удаляются:

- `src/backend/application/ports/legacy.py`;
- `src/backend/infrastructure/persistence/sqlalchemy/legacy.py`;
- `src/backend/task_management/ports.py`;
- `src/backend/task_management/repository.py`.

`tests/test_port_contracts.py` сохраняет проверку разбиения агрегатных
методов, но без `legacy.TaskManagementRepository`; литерал
`LEGACY_FAT_PORT_METHODS` переименовывается в `ALL_AGGREGATE_METHODS`.
`tests/test_composition.py` теряет импорты legacy-фасада и тест composite
adapter. `tests/test_architecture.py` убирает два удалённых пути из
`COMPATIBILITY_SHIMS`.

`FakeTaskManagementRepository` остаётся тестовым double'ом, структурно
реализующим несколько агрегатных портов. Он больше не обязан соответствовать
production composite-протоколу.

### 7.2. Reminder presenter

Новый модуль `application/task_management/reminder_presenter.py`:

```python
def build_reminder_text(
    content: ReminderContent,
    *,
    kind: ReminderKind,
    offset_minutes: int | None,
    now: datetime,
    timezone_name: str | None,
) -> str: ...
```

Из `domain/task_management/policies.py` удаляются `build_reminder_text`,
`_format_description`, `_countdown_minutes`, `_format_duration`, `_plural`,
`_format_due`, `_MESSAGE_HEADER`, `_DESCRIPTION_LIMIT`, `_MONTHS`,
`_PRIORITY_LINES` и импорт `html.escape`. `ReminderContent` остаётся в
domain. `ReminderDispatcher` импортирует presenter. Шим
`backend/task_management/reminders.py` реэкспортирует `build_reminder_text`
из нового модуля, поэтому существующие импорты тестов продолжают работать.

### 7.3. Доменные ошибки напоминаний

Новый `domain/task_management/errors.py`:

```python
class ReminderConfigurationError(ValueError):
    """Invalid reminder rule or schedule outside the supported range."""
```

`validate_reminder_inputs`, `build_task_reminder`, `build_series_reminder`,
`next_reminder_slot`, `rebuild_reminder_slot` поднимают
`ReminderConfigurationError` вместо `BadRequestError`. `policies.py` больше
не импортирует `backend.core.errors`.

Application-сервисы, вызывающие эти функции (`TaskService.create`,
`TaskMutationService.update`, `SeriesService.create/update`,
`SeriesMaterializer`, `ReminderDispatcher`), перехватывают
`ReminderConfigurationError` и поднимают `BadRequestError` с текущим
техническим сообщением; для dispatcher — обрабатывают как ошибку доставки
по существующей ветке. Никакие публичные тексты ошибок не меняются.

### 7.4. HTTP-статусы ошибок

Новый `api/errors/status.py`:

```python
ERROR_STATUS_CODES: dict[ErrorCode, int] = {...}

def status_code_for(code: ErrorCode) -> int: ...
```

`ErrorSpec` в `core/errors.py` теряет поле `status_code`; остальные поля
(`log_level`, `message`, `description`) сохраняются. `api/errors/mapping.py`
и `api/errors/openapi.py` используют `status_code_for`. Тесты
`test_error_codes.py`/`test_error_mapping.py` проверяют полноту и точность
`ERROR_STATUS_CODES`; добавляется тест, что у `ErrorSpec` нет
`status_code`.

### 7.5. Token verification

`application/ports/auth.py` получает:

```python
class TokenVerifier(Protocol):
    def verify_access_token(self, raw_token: str) -> AccessTokenClaims: ...
```

`infrastructure/auth/tokens.py` получает `JwtTokenVerifier` с конструктором
`JwtTokenVerifier(settings: Settings)`; метод делегирует
`decode_access_token(raw_token, settings=self._settings)`.

`composition/auth.py` получает
`build_token_verifier(settings: Settings) -> TokenVerifier`.
`main.create_app` сохраняет verifier в `app.state.token_verifier`.
`api/deps.get_current_access` берёт его из `request.app.state` и больше не
импортирует `backend.infrastructure.auth.tokens`.

### 7.6. Task use cases

`application/task_management/task_details.py`:

```python
class TaskDetails:
    def __init__(
        self,
        task_repository: TaskRepository,
        series_repository: SeriesRepository,
        project_reader: ProjectReader,
    ) -> None: ...
    async def detail(self, user_id: int, task: Task) -> TaskDetail: ...
    async def details(self, user_id: int, tasks: Sequence[Task]) -> Sequence[TaskDetail]: ...
    def classify_recurrence(self, series: TaskSeries | None) -> TaskRecurrence: ...
```

`application/task_management/task_references.py`:

```python
class TaskReferences:
    def __init__(
        self,
        project_reader: ProjectReader,
        label_reader: LabelReader,
        account_repository: AccountRepository,
    ) -> None: ...
    async def validate_project(self, user_id: int, project_id: int | None) -> None: ...
    async def validate_labels(
        self, user_id: int, label_ids: Sequence[int]
    ) -> tuple[int, ...]: ...
    async def user_timezone(self, user_id: int) -> ZoneInfo: ...
    def named_timezone(self, name: str) -> ZoneInfo: ...
```

`application/task_management/task_inputs.py` содержит чистые функции
`normalize_task_title`, `validate_task_due_at`,
`validate_recurrence_interval` с текущими сообщениями и границами. Это
task-specific helpers для `TaskService`/`TaskMutationService`; series
сохраняет собственные `_normalize_title`/`_resolve_schedule`, потому что
публичные тексты и правила frequency/interval у него другие.

`TaskService` сохраняет методы `list`, `today_progress`, `get`, `create`,
`complete`; зависимости — `TaskRepository`, `ReminderRepository`,
`ProjectReader` (для `today_progress`), `TaskReferences`, `TaskDetails`,
`UnitOfWork`, clock.

`TaskMutationService` (`task_mutations.py`) получает методы `update`,
`uncomplete`, `delete` и private helpers `_lock_update_target`,
`_update_detached_task`, `_apply_task_update`, `_update_series_task`,
`_shift_overdue_task`, `_rebuild_task_reminders`. Зависимости —
`TaskRepository`, `SeriesRepository`, `ReminderRepository`, `TaskReferences`,
`TaskDetails`, `SeriesOperations`, `UnitOfWork`, clock. Сигнатуры `update`
остаются текущими (keyword-only параметры), поэтому поведение и тестовые
сценарии не меняются; меняется только владелец метода.

### 7.7. Series use cases

`application/task_management/series_materializer.py`:

```python
class SeriesMaterializer:
    def __init__(
        self,
        series_repository: SeriesRepository,
        task_repository: TaskRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        unit_of_work: UnitOfWork,
    ) -> None: ...
    async def materialize(self, series: TaskSeries, now: datetime) -> int: ...
```

Сюда переносятся `_generate_series` и проверка
`ProjectReader.project_is_active` (архивный/удалённый проект не
материализуется). `_instance_reminder` выносится в чистую функцию
`build_instance_reminder` в `series_reminders.py`; её используют и
`SeriesMaterializer`, и `SeriesService._update_future_instances`, поэтому
правило слотов не дублируется. `SeriesService.create`, `create_from_task`,
`update` вызывают `materializer.materialize` в тех же точках, где раньше
вызывали `_generate_series`; flush-before-dedupe и порядок flush
сохраняются.

`application/task_management/series_generation.py`:

```python
class SeriesGenerationService:
    def __init__(
        self,
        series_repository: SeriesRepository,
        materializer: SeriesMaterializer,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...
    async def generate(self, now: datetime) -> int: ...
```

Сюда переносятся `generate` и цикл claim/commit/rollback. Проверка
активности проекта остаётся внутри `SeriesMaterializer`; отдельный
`ProjectReader` генератору не нужен. `SeriesService` больше не содержит
`generate`. `tasks/generation.py` использует
`build_series_generation_service`.

`SeriesService` сохраняет `list`, `get`, `list_details`, `get_detail`,
`create`, `create_from_task`, `update`, `detach_task`, `delete`; использует
`TaskReferences` и `SeriesMaterializer`. `TaskDetails` ему не нужен:
`SeriesDetail` собирается собственным `_detail`.

### 7.8. Project reminder resume

`domain/task_management/models.py` получает frozen dataclass:

```python
@dataclass(frozen=True)
class ProjectReminderContext:
    reminder: TaskReminder
    task_due_at: datetime | None
    series_timezone: str | None
    is_series: bool
```

`ReminderRepository` получает:

```python
async def list_project_reminder_contexts(
    self, user_id: int, project_id: int
) -> Sequence[ProjectReminderContext]: ...

async def save_reminders(self, reminders: Sequence[TaskReminder]) -> None: ...
```

и теряет `resume_project_reminders`. `pause_project_reminders` остаётся.
Адаптер `list_project_reminder_contexts` выполняет те же 4 batch-запроса,
но только читает и маппит; никаких мутаций и flush. `save_reminders`
применяет изменения ко всем строкам и делает один `flush`, сохраняя текущее
количество flush-ов; `save_reminder` остаётся для точечных изменений.

`application/ports/collaborators.py` получает:

```python
class ProjectReminderCommands(Protocol):
    async def pause_project(self, user_id: int, project_id: int) -> None: ...
    async def resume_project(
        self, user_id: int, project_id: int, now: datetime
    ) -> None: ...
```

`application/task_management/project_reminders.py` реализует
`ProjectReminderService(ProjectReminderCommands)`:
`pause_project` делегирует bulk-метод репозитория; `resume_project`
перебирает contexts, применяет `rebuild_reminder_slot`/серии-шаблоны и
сохраняет весь набор через `save_reminders` (один flush). `ProjectService`
зависит от `ProjectReminderCommands`, а не от полного `ReminderRepository`.

### 7.9. Composition root

`composition/task_management.py` получает frozen dataclass:

```python
@dataclass(frozen=True)
class TaskManagementAdapters:
    palette: PaletteRepository
    account: AccountRepository
    projects: ProjectRepository
    labels: LabelRepository
    tasks: TaskRepository
    series: SeriesRepository
    reminders: ReminderRepository
    unit_of_work: UnitOfWork
```

и приватную фабрику `_build_task_management_adapters(session)`.

`TaskManagementServices` расширяется полями `task_mutations` и
`project_reminders` там, где это нужно для API. `build_task_management_services`,
`build_series_service`, `build_series_generation_service`,
`build_reminder_dispatcher`, `build_project_reminder_service` используют
общий adapter builder.

`api/deps.py` получает `get_task_management_services(session)`;
`get_task_service`, `get_task_mutation_service`, `get_series_service`,
`get_project_service`, `get_label_service`, `get_palette_service` берут поля
одного и того же `TaskManagementServices`. FastAPI кэширует зависимость на
request, поэтому граф собирается один раз.

### 7.10. API-улучшения

- `api/routes/labels.py` в `list_labels` собирает `color_ids` и вызывает
  `palette_service.get_colors(color_ids)` один раз; маппинг остаётся в
  роуте/маппере.
- `api/routes/projects.py` удаляет `_load_summary`; роуты вызывают
  `service.get_summary` напрямую.
- `api/routes/tasks.py`: `update_task`, `uncomplete_task`, `delete_task`
  используют `TaskMutationServiceDep`; остальные — `TaskServiceDep`.
  Сигнатуры HTTP и тела ответов не меняются.

## 8. Инварианты поведения

1. **HTTP:** все пути, методы, статусы, тела успешных ответов и error
   envelope остаются прежними. `description` ошибок — те же русские строки.
2. **OpenAPI:** operation IDs, теги, query-параметры, response models и
   документированные `400/401/403/404/409/422/500/503` не меняются.
3. **БД:** ни одной миграции; таблицы, колонки, индексы, constraints и FK
   остаются прежними.
4. **Tenant isolation:** каждый tenant-scoped адаптерный запрос содержит
   `user_id`; cross-user lookup возвращает `None`/404, мутации не происходят.
5. **Транзакции:** request commit в `Database.session()`, per-series
   commit/rollback в generation, dispatcher commit order, auth
   commit-on-error, series-before-task lock order, flush-before-dedupe — без
   изменений.
6. **Производительность:** batch-запросы lists/unarchive/generation
   сохраняются; новый resume context reader делает те же 4 batch-запроса;
   маппинг не добавляет запросов. Composition собирает один граф на request
   вместо нескольких.
7. **Reminder delivery:** batch 100, soft deadline 240 s, at-least-once,
   lease/backoff/идемпотентность, commit-порядок sent-before-slot — без
   изменений.
8. **Generation:** горизонт 90 дней, `FOR UPDATE SKIP LOCKED`,
   идемпотентность по `(series_id, due_at)`, завершённые серии не
   выбираются.
9. **Auth:** refresh rotation/reuse/grace, cookie-флаги, origin-guard,
   commit-on-error — без изменений. `TokenVerifier` возвращает те же claims и
   ошибки, что `decode_access_token`.
10. **Admin:** immutable slug, last-active-color, FK conflict — без
    изменений.
11. **Логирование:** те же события/поля; raw token/init data не логируются.

## 9. Изменения по слоям

- **domain:** новый `errors.py`; `ProjectReminderContext`; удаление текста
  уведомлений и `core.errors` из policies.
- **application:** новые `task_details.py`, `task_inputs.py`,
  `task_mutations.py`, `task_references.py`, `series_generation.py`,
  `series_materializer.py`, `series_reminders.py`, `project_reminders.py`,
  `reminder_presenter.py`; обновление `tasks.py`, `series.py`,
  `reminders.py`, портов.
- **infrastructure:** `JwtTokenVerifier`; `list_project_reminder_contexts`
  вместо `resume_project_reminders`; удаление `legacy.py`.
- **composition:** общий adapter builder, новые фабрики, единый граф.
- **api:** `TokenVerifier` через `app.state`; новые зависимости;
  batch-палитра; удаление passthrough; `api/errors/status.py`.
- **core:** `ErrorSpec` без `status_code`.
- **tests:** обновление импортов/точек сборки, новые architecture/contract
  тесты; поведенческие assertions не меняются.
- **docs:** README/AGENTS — удалить упоминания удалённого composite-фасада,
  зафиксировать новый composition-контракт.

## 10. Тестовая стратегия

- TDD: каждая задача начинается с падающего теста.
- **Architecture tests:** запрет `backend.core.errors` в `domain`; запрет
  `backend.infrastructure` и `backend.models` в `api`; запрет
  `composition` во внутренних слоях; проверка, что удалённые legacy-модули
  отсутствуют и не импортируются.
- **Port contract tests:** агрегатные протоколы сохраняют точный набор
  методов; новый `TokenVerifier` документирован; `ProjectReminderCommands`
  узкий; `resume_project_reminders` отсутствует в `ReminderRepository`;
  `list_project_reminder_contexts` и `save_reminders` документированы.
- **Domain tests:** `ReminderConfigurationError` вместо `BadRequestError`;
  `build_reminder_text` отсутствует в domain и доступен из application
  presenter через compatibility-шим; доменные политики не импортируют
  `core.errors`.
- **Error tests:** `ERROR_STATUS_CODES` покрывает все `ErrorCode`; wire
  envelope, `description` и `request_id` не изменились; `ErrorSpec` не
  содержит `status_code`.
- **Auth tests:** `JwtTokenVerifier` принимает/отклоняет те же токены, что
  `decode_access_token`; `api/deps` использует `app.state.token_verifier`;
  истёкший/битый токен возвращает тот же `401`.
- **Service tests:** существующие `test_task_management_tasks.py`/
  `test_task_management_series.py` переводятся на новых владельцев методов;
  assertions поведения, commit/flush counts и порядок вызовов не меняются.
- **Generation tests:** `SeriesGenerationService` сохраняет per-series
  commit/rollback, candidate snapshot, `SKIP LOCKED` и идемпотентность;
  `SeriesMaterializer` даёт те же задачи/labels/reminders, что раньше.
- **Reminder resume tests:** адаптер только читает contexts; сервис
  применяет политику; количество запросов не растёт; archived→active
  восстанавливает те же слоты.
- **Composition tests:** один граф на request; все сервисы делят session и
  UoW; worker-фабрики используют общий adapter builder.
- **API tests:** batch-палитра вызывает `get_colors` один раз; списки меток
  и проектов возвращают те же JSON; route-auth matrix не меняется.
- Тесты не требуют PostgreSQL, NATS или Redis.

## 11. Совместимость и миграция

- Порядок задач: независимые cleanup-задачи → общие collaborators → split
  task → split series → resume policy → composition → API → финальные
  guards.
- Старые тонкие шимы остаются; удаляются только `ports.py`/`repository.py`
  и composite-фасад, у которых нет production-потребителей.
- При откате одной задачи достаточно вернуть предыдущий commit: новые
  модули не меняют БД и не создают миграций.
- Высокорисковые зоны:
  - **Generation:** сохранить flush-before-dedupe, per-series commit и
    candidate snapshots; покрыть `test_task_generation.py` и series-тесты.
  - **Reminder resume:** сохранить 4 batch-запроса и правила слотов;
    покрыть `test_task_management_projects.py`/`test_task_management_reminders.py`.
  - **Reminder text:** тексты и HTML-escaping должны совпадать байт-в-байт;
    тесты `test_task_management_reminders.py` остаются.
  - **Auth:** verifier должен возвращать те же `AccessTokenClaims` и ошибки.
  - **Composition:** один граф на request не должен менять commit/flush
    поведение; `test_deps.py` остаётся.

## 12. Проверка результата

1. `poetry run pytest -q` — все тесты зелёные.
2. `poetry run ruff check .` — без ошибок.
3. `poetry run ruff format --check src tests` — без изменений.
4. `poetry run mypy src tests` — без ошибок.
5. `poetry run pytest --cov=backend --cov-report=term-missing` — покрытие не
   падает.
6. Архитектурные тесты подтверждают: domain не импортирует core.errors;
   api не импортирует infrastructure/models; composition не импортируется
   внутренними слоями; удалённые legacy-модули отсутствуют.
7. OpenAPI contract test и `tests/test_route_auth_matrix.py` подтверждают
   неизменность HTTP-контракта.
8. `graphify update .` — граф обновлён.
9. `git diff` не содержит изменений схемы БД, Alembic-миграций, middleware
   order, Taskiq schedules, cookie-флагов и публичных текстов ошибок.

## 13. Отложено

Следующие улучшения из ревью сознательно не входят в эту итерацию:

- типизированные `TaskUpdateCommand`/`SeriesUpdateCommand` вместо
  `dict[str, Any]`;
- дробление `ReminderDispatcher.dispatch_due` (165 строк) и
  `AuthService.refresh` (125 строк);
- дробление `tests/task_management_fakes.py` на узкие fakes;
- удаление остальных compatibility-шимов;
- дальнейшее сужение агрегатных портов (`ProjectService` → узкий
  `ProjectReminderCommands` уже входит, остальные — по необходимости);
- оптимизации, не связанные с перечисленными N+1/ composition-графом.

Эти пункты фиксируются как известный долг и могут быть оформлены отдельной
спекой после стабилизации текущей итерации.
