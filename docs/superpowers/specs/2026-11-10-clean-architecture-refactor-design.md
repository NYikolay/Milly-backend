# Переход backend на чистую архитектуру

- Дата: 2026-11-10
- Статус: проект, ожидает утверждения
- Область: `backend/` (FastAPI, Python 3.14, SQLAlchemy 2.x, PostgreSQL, Taskiq)
- Потребитель контракта: Telegram Mini App клиент (API-поведение не меняется)

## 1. Зачем это нужно

Слои сейчас разделены только по именам пакетов. Фактическая зависимость идёт
не внутрь, а в обе стороны:

1. `task_management/ports.py` импортирует восемь ORM-моделей и возвращает их
   из 77-методного `TaskManagementRepository` (`ports.py:8-15,18`). Сервисы
   импортируют те же ORM-модели (`tasks.py:11-15`, `projects.py:9-10`,
   `series.py:12-16`, `labels.py:7`, `palette.py:6`, `reminders.py:15-16`),
   поэтому use-case-слой нельзя запустить без SQLAlchemy.
2. `task_management/repository.py` — адаптер на 2651 строку, который кроме SQL
   содержит доменные правила: пересчёт reminder-слотов, горизонт генерации,
   каскад удаления проекта. Он же вызывает `commit`/`rollback`
   (`repository.py:2605-2619`), хотя порт утверждает обратное (`ports.py:27-28`).
3. ORM-сущности пересекают границу API: `ReminderResponse.from_row(TaskReminder)`
   (`task_schemas.py:11,405-429`), сериализация label (`routes/labels.py:15,28-48`),
   `from_attributes` для auth (`auth/schemas.py:19,36`), `get_current_user`
   возвращает ORM `User` (`api/deps.py:271-299`).
4. Composition root продублирован: сервисный граф собирается в `api/deps.py:146-198`,
   в `tasks/generation.py:44-50` и `tasks/delivery.py:44-49`; уже есть расхождения.
5. Auth одновременно является доменом, persistence и HTTP: сервис импортирует
   ORM (`auth/service.py:21-22`), репозиторий коммитит на error-path
   (`auth/repository.py:608-653`), `auth/cookies.py:3` импортирует FastAPI.
6. `admin/views.py:116-214` дублирует политику «последнего активного цвета»
   вместо переиспользования `PaletteService` (`palette.py:158-227`).
7. Направление `models -> task_management.enums` (`task.py:26`,
   `task_series.py:29`, `task_reminder.py:26`, `reminder_delivery.py:23`) вместе
   с обратным `task_management -> models` образует пакетный цикл.

Следствие: изменение схемы БД или SQLAlchemy задевает use-case-тесты, API
нельзя тестировать без ORM, бизнес-правила невозможно переиспользовать в
admin/worker, а транзакционная семантика размазана между сервисами, адаптером
и dependency.

## 2. Цели

1. Выделить `domain`: ORM-free сущности, value objects, enum'ы и чистые
   политики (даты, расписания, reminder-слоты, рендеринг текста).
2. Выделить `application`: use-case-сервисы, DTO и узкие порты. Ни один
   application-модуль не импортирует FastAPI, SQLAlchemy, Taskiq, ORM или
   concrete-адаптеры.
3. Разделить 77-методный порт по агрегатам: palette, account, project, label,
   task, series, reminder. Ввести `UnitOfWork` для `flush`/`commit`/`rollback`.
4. Перенести SQLAlchemy-адаптеры и маппинг ORM <-> domain в
   `infrastructure/persistence/sqlalchemy`. Сервисы работают только с domain.
5. Убрать ORM из API: Pydantic-схемы описывают транспорт, мапперы
   `domain -> response` живут в `api/mappers`.
6. Собрать один composition root для API, Taskiq и admin. Concrete-адаптеры
   выбираются только там.
7. Разделить auth на domain/application/infrastructure/HTTP-adapter.
8. Переиспользовать palette-политику в SQLAdmin без дублирования SQL.
9. Зафиксировать архитектурные правила тестами (AST-проверка импортов) и
   контрактными тестами портов/мапперов.
10. Сохранить без изменений HTTP-контракты, схему БД, транзакционные границы,
    производительность и бизнес-поведение.

## 3. Не-цели

- Не менять URL, HTTP-методы, коды ответов, формы JSON, error envelope,
  `request_id`, cursor-формат, OpenAPI operation ID и документированные ошибки.
- Не менять схему БД и не добавлять Alembic-миграции.
- Не менять бизнес-правила: recurrence, reminder slots, пагинацию, soft/hard
  delete, archive/unarchive, права доступа, лимиты, тексты ошибок.
- Не менять транзакционные границы: request commit, per-series commit,
  per-delivery commit, auth commit-on-error, lock order series-before-task,
  flush-before-dedupe.
- Не добавлять runtime/dev-зависимости.
- Не менять порядок middleware, Taskiq schedules, cookie-флаги и CORS.
- Не разбивать backend на сервисы/процессы и не менять Docker/compose.
- Не переписывать frontend и не менять формат Telegram init data.
- Не менять at-least-once семантику доставки напоминаний.
- Не удалять поведенческие тесты; допускается только их перевод на domain-типы
  и новые пути импорта.

## 4. Критерии неудачи

- Любой модуль `backend.domain` или `backend.application` импортирует
  `fastapi`, `starlette`, `sqlalchemy`, `taskiq`, `pydantic`,
  `pydantic_settings`, `jwt`, `backend.models`, `backend.db`,
  `backend.infrastructure`, `backend.api`, `backend.tasks`, `backend.admin`,
  `backend.composition`, `backend.core.config`, `backend.core.middleware`,
  `backend.task_management` или `backend.auth`.
- Сервис импортирует ORM-модель, SQLAlchemy-сессию или concrete-адаптер.
- `TaskManagementRepository` остаётся единственным портом, который используют
  сервисы после завершения рефакторинга.
- API-схемы или роуты импортируют ORM-модели (кроме тестов моделей и admin).
- Изменился хотя бы один HTTP-контракт: путь, метод, статус, JSON-поле,
  error code/description, cursor, operation ID или документированный набор
  ошибок.
- Изменилась транзакционная семантика: ранний/поздний commit, потеря
  commit-on-error, иной порядок блокировок, иная идемпотентность.
- Появился N+1 или лишний запрос на списках tasks/projects, при unarchive
  проекта или при генерации серий.
- Нарушена tenant-изоляция: запрос без `user_id` или cross-user данные
  становятся видимыми.
- Изменилось поведение SQLAdmin с palette (последний активный цвет, slug,
  foreign-key конфликт).
- Падают `pytest`, `ruff`, `ruff format --check`, `mypy`.
- `graphify update .` не выполнен после изменений.

## 5. Принятые решения

1. **Strangler-миграция, не big-bang.** Новые слои вводятся рядом со старыми.
   Старые модули на время миграции остаются рабочими; в конце становятся
   тонкими re-export-шимами. Каждая задача плана оставляет полный набор тестов
   зелёным.
2. **Направление зависимостей:**
   `domain <- application <- infrastructure/api/tasks/admin/composition`.
   `core` — shared kernel; `models` и `db` — infrastructure. `composition`
   зависит от всех слоёв, но никто не зависит от `composition`.
3. **Domain-сущности — изменяемые dataclass'ы**, повторяющие persisted-поля
   ORM, без SQLAlchemy. Скалярные типы сохраняются (`priority: str`,
   `frequency: str`), enum'ы используются на границах use-case. Для transient
   записей `id: int = 0`; адаптер вставляет строку при `id == 0` и обновляет
   при `id != 0`. `created_at`/`updated_at` имеют default factory на текущий
   UTC; БД может перезаписать значения при вставке.
4. **Явные save-методы.** Сервис после мутации сущности вызывает
   `save_task`/`save_series`/`save_project`/`save_label`/`save_reminder`/
   `save_delivery`. Адаптер не коммитит и не отслеживает detached-объекты
   неявно. Это делает persistence-эффекты видимыми и тестируемыми.
5. **Порты по агрегатам.** `TaskManagementRepository` остаётся только
   compatibility-фасадом для старых тестов; application-сервисы зависят от
   узких протоколов. Полный состав — в разделе 7.3.
6. **`UnitOfWork` — единственный владелец commit/rollback в application.**
   `flush` разрешён адаптерам и сервисам. Request-level commit остаётся в
   `Database.session()`; сервисы в обычном request-flow не коммитят.
7. **SQLAlchemy-маппинг — в infrastructure.** Функции `to_entity`/`apply_entity`
   для каждой сущности; никакого lazy loading. Связанные данные загружаются
   явными batch-запросами, как сейчас.
8. **API-мапперы — в `api/mappers`.** Pydantic-схемы не знают ORM; роуты
   вызывают сервис, затем маппер, затем возвращают response model. Cursor-
   политика остаётся transport-level и переносится в `api/queries`.
9. **Composition root — `backend/composition/`.** `api/deps.py`,
   `tasks/generation.py`, `tasks/delivery.py` и admin используют одни и те же
   фабрики. Concrete-адаптеры создаются только в composition.
10. **Auth:** domain-сущности `User`, `TelegramProfile`, `AuthSession`,
    `TelegramUser`, `TelegramIdentity`; порты `AuthRepository`,
    `InitDataValidator`, `TokenIssuer`; SQLAlchemy-адаптер, Pydantic-валидатор
    init data и JWT-адаптер — в infrastructure; cookie-функции — в
    `api/auth/cookies.py`, Pydantic auth-схемы — в `api/auth/schemas.py`.
    `AuthService` получает `AuthSettings` (plain dataclass), а не Pydantic
    `Settings`, и не импортирует Pydantic/FastAPI. Commit-on-error выполняется
    через общий `UnitOfWork`.
11. **Admin:** `ColorPaletteAdmin` строит `PaletteService` на своей сессии и
    вызывает `ensure_can_deactivate`/`ensure_can_delete`; SQL-подсчёт и
    дублированная политика удаляются. Проверка immutable slug остаётся в admin,
    так как это admin-специфичное правило.
12. **Совместимость импортов:** старые пути остаются тонкими
    re-export-шимами: все модули `backend.task_management.*`
    (`enums`, `date_bounds`, `schedule`, `palette`, `labels`, `projects`,
    `series`, `tasks`, `reminders`, `ports`, `repository`), все модули
    `backend.auth.*` (`service`, `repository`, `cookies`, `tokens`,
    `init_data`, `errors`, `schemas`) и `backend.tasks.telegram`. Внутренние
    слои их не импортируют; реальные модули живут в новых пакетах.
13. **Архитектурные тесты:** AST-проверка импортов для `domain`/`application`;
    проверка отсутствия ORM в `api/task_schemas.py` и `api/routes`; проверка,
    что fake и SQLAlchemy-адаптеры реализуют все методы портов.
14. **Тесты:** поведенческие тесты сохраняются; фабрики `make_task`,
    `make_series`, `make_project`, `make_label`, `make_reminder`,
    `make_delivery` переводятся на domain-сущности. ORM-тесты моделей
    (`test_task_management_models.py`, `test_auth_models.py`) остаются ORM.
15. **Производительность:** batch-запросы и bulk-flush сохраняются; маппинг
    выполняется в памяти и не добавляет запросов.

## 6. Целевая структура

```
src/backend/
  core/                         # shared kernel: errors, logging, config, middleware
  domain/
    __init__.py
    enums.py                    # Priority, TaskRecurrence, SeriesFrequency, ReminderKind, DeliveryStatus
    entities.py                 # User, TelegramProfile, AuthSession, ColorPalette, Label, Project,
                                # TaskSeries, Task, TaskReminder, ReminderDelivery, TaskLabel, SeriesLabel
    task_management/
      __init__.py
      models.py                 # TaskFilters, TaskDetail, TaskListCursor, TaskPage, ProjectProgress,
                                # ProjectSummary, ProjectListCursor, ProjectPage, SeriesDetail,
                                # ReminderInput, ReminderContent
      policies.py               # date_bounds, schedule, reminder slot/text, validation
    auth/
      __init__.py
      models.py                 # AccessTokenClaims, TokenPair, LoginResult, TelegramUser,
                                # TelegramIdentity, AuthSettings
      tokens.py                 # generate_refresh_token, hash_refresh_token (stdlib only)
  application/
    __init__.py
    ports/
      __init__.py
      unit_of_work.py           # UnitOfWork (shared by auth and task management)
      task_management.py        # PaletteRepository, AccountRepository, ProjectRepository,
                                # LabelRepository, TaskRepository, SeriesRepository,
                                # ReminderRepository
      collaborators.py          # ProjectReader, LabelReader, TaskReader, SeriesReader,
                                # SeriesOperations, PalettePolicy, ReminderSender
      auth.py                   # AuthRepository, InitDataValidator, TokenIssuer
    task_management/
      palette.py, labels.py, projects.py, series.py, tasks.py, reminders.py
    auth/
      service.py, errors.py
  infrastructure/
    __init__.py
    persistence/
      __init__.py
      sqlalchemy/
        __init__.py
        mappers.py
        palette_repository.py
        account_repository.py
        project_repository.py
        label_repository.py
        task_repository.py
        series_repository.py
        reminder_repository.py
        unit_of_work.py
        legacy.py               # composite-фасад для совместимости
        auth_repository.py
    auth/
      init_data.py              # PydanticInitDataValidator -> InitDataValidator
      tokens.py                 # JwtTokenIssuer -> TokenIssuer
    telegram/
      sender.py
  api/
    mappers/
      task_management.py
      auth.py
    queries/
      tasks.py
      projects.py
    auth/
      cookies.py
      schemas.py                # Pydantic auth-схемы
    deps.py
    routes/...
    task_schemas.py             # только Pydantic, без ORM
    pagination.py
  composition/
    __init__.py
    task_management.py
    auth.py
  models/                       # ORM-модели, только infrastructure/admin/migrations
  db/                           # engine/session, infrastructure
  tasks/                        # Taskiq entrypoints, используют composition
  admin/                        # SQLAdmin adapters, используют composition/application
```

`models/` остаётся на месте, чтобы не ломать Alembic, admin и существующие
SQL-compile тесты. Важен не путь, а направление импортов: `models` не
импортируется из `domain`/`application`.

## 7. Контракты слоёв

### 7.1. Domain-сущности

Все сущности — `@dataclass` (mutable), без SQLAlchemy и без lazy relationships.
Порядок полей в коде может отличаться от порядка колонок: обязательные поля
идут первыми, чтобы не нарушать правило Python «поля с default — после полей
без default». Имена и типы persisted-полей при этом не меняются.

| Сущность | Поля |
| --- | --- |
| `User` | `id=0`, `timezone="UTC"`, `created_at`, `updated_at`, `telegram_profile: TelegramProfile \| None = None` |
| `TelegramProfile` | `id=0`, `user_id`, `telegram_id`, `is_bot=None`, `first_name`, `last_name=None`, `username=None`, `language_code=None`, `is_premium=False`, `added_to_attachment_menu=None`, `allows_write_to_pm=None`, `photo_url=None` |
| `AuthSession` | `id=0`, `user_id`, `family_id: UUID`, `refresh_token_hash`, `created_at`, `expires_at`, `revoked_at=None` |
| `ColorPalette` | `id=0`, `slug`, `name`, `hex`, `sort_order`, `is_active=True`, `created_at`, `updated_at` |
| `Label` | `id=0`, `user_id`, `name`, `color_id`, `deleted_at=None`, `created_at`, `updated_at` |
| `Project` | `id=0`, `user_id`, `name`, `description=None`, `color_id`, `icon=None`, `archived_at=None`, `deleted_at=None`, `deletion_batch_id: UUID \| None=None`, `created_at`, `updated_at` |
| `TaskSeries` | `id=0`, `user_id`, `project_id=None`, `title`, `description=None`, `priority="neither"`, `frequency="daily"`, `interval_days=1`, `time_of_day: time`, `timezone`, `starts_on: date`, `ends_on: date \| None=None`, `generated_through=None`, `reminders_enabled=False`, `deleted_at=None`, `deletion_batch_id=None`, `created_at`, `updated_at` |
| `Task` | `id=0`, `user_id`, `series_id=None`, `project_id=None`, `title`, `description=None`, `priority="neither"`, `due_at`, `completed_at=None`, `reminders_enabled=False`, `deleted_at=None`, `deletion_batch_id=None`, `created_at`, `updated_at` |
| `TaskReminder` | `id=0`, `user_id`, `task_id=None`, `series_id=None`, `kind: str`, `enabled=True`, `offset_minutes=None`, `interval_days=None`, `time_of_day=None`, `timezone=None`, `next_run_at=None`, `created_at`, `updated_at` |
| `ReminderDelivery` | `id=0`, `reminder_id`, `user_id`, `scheduled_for`, `status="pending"`, `attempts=0`, `locked_at=None`, `next_attempt_at=None`, `sent_at=None`, `telegram_message_id=None`, `last_error=None`, `created_at`, `updated_at` |
| `TaskLabel` | `task_id`, `label_id`, `user_id`, `created_at` |
| `SeriesLabel` | `series_id`, `label_id`, `user_id`, `created_at` |

`created_at`/`updated_at` — `field(default_factory=lambda: datetime.now(UTC))`.
Для `TaskLabel`/`SeriesLabel` `id` нет (составной PK).

### 7.2. Domain DTO и enum'ы

- `domain/enums.py`: `Priority`, `SeriesFrequency`, `TaskRecurrence`,
  `ReminderKind`, `DeliveryStatus` (перенос из `task_management/enums.py`).
  Старый модуль реэкспортирует их.
- `domain/task_management/models.py`: `TaskFilters`, `TaskDetail`,
  `TaskListCursor`, `TaskPage`, `ProjectProgress`, `ProjectSummary`,
  `ProjectListCursor`, `ProjectPage`, `SeriesDetail`, `ReminderInput`,
  `ReminderContent`, `UNSET`/`UnsetType`.
- `domain/auth/models.py`: `AccessTokenClaims`, `TokenPair`, `LoginResult`
  (`user: User`, `profile: TelegramProfile`), `TelegramUser` (plain dataclass,
  без Pydantic), `TelegramIdentity` (`user: User`, `profile: TelegramProfile`),
  `AuthSettings` (telegram bot token, JWT secret/issuer/leeway, init-data
  max age/skew, access/refresh TTL, refresh grace; plain scalars, без Pydantic).
- `domain/auth/tokens.py`: `generate_refresh_token`, `hash_refresh_token`
  (stdlib only).
- `/auth/me` и `CurrentUserDep` используют domain `User`; отдельный DTO не
  вводится, потому что `User` уже содержит `id`, `timezone`, `created_at`,
  `telegram_profile`.

### 7.3. Application-порты

Существующие имена методов, параметры (кроме ORM-типов), tenant-scoping,
locking, ordering и side effects сохраняются. Меняются только типы
`ORM -> domain` и добавляются `save_*` там, где сервис мутирует сущность.

**`PaletteRepository`** (7 методов):
`list_palette_colors`, `list_active_palette_colors`, `get_palette_color`,
`get_palette_colors_by_ids`, `get_default_palette_color`,
`count_active_palette_colors`, `palette_color_is_used`.
Возвращают `ColorPalette | Sequence[ColorPalette] | dict[int, ColorPalette]`.

**`AccountRepository`** (2):
`get_user_timezone(user_id) -> str | None`,
`get_telegram_id(user_id) -> int | None`.

**`ProjectRepository`** (13):
`list_projects`, `get_project`, `get_projects_by_ids`, `project_name_exists`,
`add_project`, `save_project`, `count_projects`, `project_progress`,
`project_progress_for_projects`, `today_progress`, `planned_tasks_count`,
`project_is_active`, `soft_delete_project`.
Возвращают `Project | Sequence[Project] | dict[int, Project]`, кроме
`list_projects`: он сохраняет текущий контракт
`Sequence[tuple[Project, str]]`, где второй элемент — SQL-вычисленный
`lower(name)` для cursor. Переносить вычисление `name_key` в Python нельзя:
это изменит cursor-поведение для non-ASCII имён.

**`LabelRepository`** (7):
`list_labels`, `get_label`, `get_labels_by_ids`, `label_name_exists`,
`add_label`, `save_label`, `soft_delete_label`.
Возвращают `Label | Sequence[Label] | dict[int, Label]`.

**`TaskRepository`** (14):
`list_tasks`, `get_task`, `add_task`, `add_tasks`, `save_task`,
`soft_delete_task`, `hard_delete_tasks`, `list_task_labels_for_tasks`,
`list_task_reminders_for_tasks`, `add_task_labels`, `add_task_label_rows`,
`replace_task_labels`, `list_task_labels`, `list_future_series_tasks`.
Возвращают `Task | Sequence[Task] | dict[int, Sequence[Label]] |
dict[int, Sequence[TaskReminder]]`.

**`SeriesRepository`** (14):
`list_series`, `get_series`, `get_series_by_ids`, `add_series`, `save_series`,
`soft_delete_series`, `add_series_labels`, `replace_series_labels`,
`list_series_labels`, `list_series_for_generation`,
`claim_series_for_generation`, `get_series_tasks_for_update`,
`series_due_slot_exists`, `list_series_due_times`.
Возвращают `TaskSeries | Sequence[TaskSeries] | Sequence[Task]`.
Проверка активности проекта идёт через `ProjectReader`, а вставка
task-label/reminder-строк — через `TaskRepository`/`ReminderRepository`.

**`ReminderRepository`** (23):
`list_task_reminders`, `list_series_reminders`, `add_reminder`,
`add_reminders`, `save_reminder`, `delete_task_reminders`,
`delete_series_reminders`, `cancel_task_reminders`, `cancel_series_reminders`,
`pause_project_reminders`, `resume_project_reminders`, `list_due_reminders`,
`claim_due_reminder`, `update_reminder_next_run_at`, `get_delivery`,
`add_delivery`, `save_delivery`, `mark_delivery_sending`,
`mark_delivery_sent`, `mark_delivery_pending`, `mark_delivery_failed`,
`cancel_delivery`, `reactivate_delivery`.
Возвращают `TaskReminder | ReminderDelivery | Sequence[...]`.

**`UnitOfWork`** (3, shared module `application/ports/unit_of_work.py`):
`flush() -> None`, `commit() -> None`, `rollback() -> None`.
`task_management.py` и `auth.py` реэкспортируют его для удобства.

**Collaborator-порты** (узкие интерфейсы вместо concrete-сервисов):
- `ProjectReader`: `get_project`, `project_is_active`, `get_projects_by_ids`,
  `today_progress`.
- `LabelReader`: `get_label`, `get_labels_by_ids`.
- `TaskReader`: `get_task`.
- `SeriesReader`: `get_series`.
- `SeriesOperations`: `get`, `get_detail`, `create_from_task`, `delete`,
  `update`, `detach_task`.
- `PalettePolicy`: `get_color`, `get_colors`, `resolve_for_write`,
  `ensure_active`, `ensure_can_deactivate`, `ensure_can_delete`.
- `ReminderSender`: существующий `send(chat_id, text)`.
- `InitDataValidator`: `validate(raw_init_data: str) -> TelegramUser`.
- `TokenIssuer`: `issue_access_token(user_id, session_id, telegram_id, *, now) ->
  tuple[str, datetime]`.
- `Clock` остаётся `Callable[[], datetime]`; отдельный Protocol не вводится.

**`AuthRepository`** (domain-типы):
`acquire_telegram_lock`, `get_identity_by_telegram_id(telegram_id) ->
TelegramIdentity | None`, `create_user_with_profile(data: TelegramUser) ->
TelegramIdentity`, `update_profile(profile: TelegramProfile, data: TelegramUser)`,
`revoke_user_sessions`, `create_session`, `get_telegram_id_for_session`,
`get_session_for_update`, `revoke_session`, `revoke_family`,
`get_user_with_profile`, `update_user_timezone`.
Commit-хелперы `revoke_*_and_commit` удаляются; коммит делает `AuthService`
через `UnitOfWork`.

**Compatibility-фасад:** `application/ports/legacy.py` объявляет
`class TaskManagementRepository(PaletteRepository, ProjectRepository,
LabelRepository, TaskRepository, SeriesRepository, ReminderRepository,
AccountRepository, UnitOfWork, Protocol)`, а старый
`task_management/ports.py` только реэкспортирует его. Application-модули
фасад не используют; он нужен только старым тестам и периоду миграции.

### 7.4. Транзакционная политика

- `UnitOfWork` — единственный порт, через который application делает
  `commit`/`rollback`. Адаптеры коммитить не могут.
- Request-flow: `Database.session()` (`db/session.py:62-91`) остаётся
  владельцем финального commit/rollback. Сервисы только `flush`.
- `SeriesService.generate`: `uow.commit()` после каждой серии,
  `uow.rollback()` при ошибке; candidate ids снимаются до обработки
  (`series.py:719-751`).
- `ReminderDispatcher`: claim фиксируется коммитом только для пропущенных
  кандидатов (`claim_due_reminder` вернул `None` или `next_run_at is None`);
  успешная отправка делает три коммита — после `mark_delivery_sending`, после
  `mark_delivery_sent` и после обновления слота; остальные исходы коммитятся
  после обработки; rollback при неожиданной ошибке
  (`application/task_management/reminders.py:123-254`).
- Auth refresh: `revoke_session`/`revoke_family`, затем `uow.commit()`, затем
  `raise` (`auth/service.py:232-260`). Нормальный login/logout не коммитит.
- Lock order: series before tasks (`tasks.py:884-943`,
  `repository.py:2108-2140`).
- `create_from_task` сохраняет flush перед генерацией
  (`series.py:386-389`), чтобы dedupe по `(series_id, due_at)` видел задачу.
- Никаких новых транзакционных границ, savepoints или «одного commit на всё».

### 7.5. Infrastructure: маппинг и адаптеры

- `mappers.py`:
  `user_to_entity/apply_user`, `profile_to_entity/apply_profile`,
  `auth_session_to_entity/apply_auth_session`,
  `color_to_entity/apply_color`, `label_to_entity/apply_label`,
  `project_to_entity/apply_project`, `series_to_entity/apply_series`,
  `task_to_entity/apply_task`, `reminder_to_entity/apply_reminder`,
  `delivery_to_entity/apply_delivery`, `task_label_to_entity`,
  `series_label_to_entity`.
- `apply_*` копирует persisted-поля из domain в ORM; relationship-поля
  (`telegram_profile`) не маппятся автоматически.
- Адаптеры принимают `AsyncSession`; `UnitOfWork` — общий
  `SqlAlchemyUnitOfWork(session)`.
- `legacy.py` содержит `SqlAlchemyTaskManagementRepository`, который
  делегирует агрегатным адаптерам и реализует compatibility-фасад.
- SQL-запросы, tenant-фильтры, `FOR UPDATE`, `SKIP LOCKED`, `LIMIT`,
  `ORDER BY`, bulk-вставки и flush-точки сохраняются один в один.

### 7.6. API: мапперы и тонкие роуты

- `api/mappers/task_management.py`:
  `task_response(TaskDetail) -> TaskResponse`,
  `task_list_response(TaskPage, *, next_cursor: str | None = None) -> TaskListResponse`,
  `series_response(SeriesDetail) -> SeriesResponse`,
  `project_response(ProjectSummary) -> ProjectResponse`,
  `label_response(Label, ColorPalette) -> LabelResponse`,
  `color_response(ColorPalette) -> ColorResponse`,
  `reminder_response(TaskReminder) -> ReminderResponse`.
- `api/mappers/auth.py`:
  `user_response(User) -> UserResponse`,
  `profile_response(TelegramProfile) -> TelegramProfileResponse`,
  `auth_response(LoginResult, expires_in) -> AuthResponse`,
  `token_response(TokenPair, expires_in: int) -> TokenResponse`.
- `api/queries/tasks.py`/`projects.py`: cursor parsing/encoding и
  fingerprint-политика, перенесённые из роутов без изменения формата.
- Роуты: только dependency -> service -> mapper -> response. Никаких
  `model_validate(ORM)` и ORM-импортов.
- `CurrentUserDep` возвращает domain `User`, а не ORM `User`.

### 7.7. Composition root

`composition/task_management.py`:
```python
@dataclass(frozen=True)
class TaskManagementServices:
    palette: PaletteService
    projects: ProjectService
    labels: LabelService
    series: SeriesService
    tasks: TaskService

def build_task_management_services(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] | None = None,
) -> TaskManagementServices: ...

def build_series_service(
    session: AsyncSession,
    *,
    clock: Callable[[], datetime] | None = None,
) -> SeriesService: ...

def build_reminder_dispatcher(
    session: AsyncSession,
    sender: ReminderSender,
    *,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ReminderDispatcher: ...
```
`composition/auth.py`:
```python
def build_auth_service(
    session: AsyncSession,
    settings: Settings,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AuthService:
    ...
    # Maps Settings -> AuthSettings, then constructs
    # PydanticInitDataValidator, JwtTokenIssuer,
    # SqlAlchemyAuthRepository, SqlAlchemyUnitOfWork.
```
`api/deps.py` использует эти фабрики. `tasks/generation.py` вызывает
`build_series_service`, `tasks/delivery.py` — `build_reminder_dispatcher`.
Admin использует `build_palette_service`/`PaletteService` на своей сессии.
Дублирующих графов больше нет.

### 7.8. Auth-границы

- Domain: `User`, `TelegramProfile`, `AuthSession`, `TelegramUser`,
  `TelegramIdentity`, `AccessTokenClaims`, `TokenPair`, `LoginResult`,
  `AuthSettings`; pure `generate_refresh_token`/`hash_refresh_token`.
- Application: `AuthService(repository, unit_of_work, validator, token_issuer,
  settings: AuthSettings, clock)` зависит от `AuthRepository`,
  `InitDataValidator`, `TokenIssuer`, `UnitOfWork`. Не импортирует ORM,
  Pydantic, FastAPI.
- Infrastructure: `SqlAlchemyAuthRepository` маппит ORM <-> domain;
  `PydanticInitDataValidator` реализует `InitDataValidator` и использует
  Pydantic только внутри adapter (принимает `Settings`); `JwtTokenIssuer`
  реализует `TokenIssuer` и инкапсулирует PyJWT + `Settings`. Module-level
  `validate_init_data`, `create_access_token`, `decode_access_token`,
  `generate_refresh_token`, `hash_refresh_token` остаются в infrastructure
  для совместимости старых импортов.
- HTTP: `api/auth/cookies.py` — FastAPI `Request`/`Response`, cookie-флаги,
  origin-guard; `api/auth/schemas.py` — Pydantic auth-схемы.
  `backend/auth/cookies.py` и `backend/auth/schemas.py` — re-export-шимы.
- `composition/auth.py` — единственное место, где `Settings` превращается в
  `AuthSettings` и выбираются concrete-адаптеры.

### 7.9. Admin

- `ColorPaletteAdmin.on_model_change`: immutable slug остаётся; проверка
  «не последний активный» вызывает
  `PaletteService.ensure_can_deactivate(model.id)` на сессии admin.
- `on_model_delete`: `PaletteService.ensure_can_delete(model.id)`.
- `delete_model`: перехват `IntegrityError` сохраняется.
- Никакого прямого `select(func.count())` в admin.

## 8. Инварианты поведения

1. **HTTP:** все пути, методы, статусы, тела успешных ответов и error
   envelope остаются прежними. `description` ошибок — те же русские строки.
   `request_id` присутствует и совпадает с `X-Request-ID`.
2. **OpenAPI:** operation IDs, теги, query-параметры, response models и
   документированные `400/401/403/404/409/422/500/503` не меняются.
3. **БД:** ни одной миграции; таблицы, колонки, индексы, constraints,
   partial indexes и FK остаются прежними.
4. **Tenant isolation:** каждый tenant-scoped адаптерный запрос содержит
   `user_id`; cross-user lookup возвращает `None`/404, мутации не происходят.
   Системные запросы без `user_id` явно ограничены: чтение глобальной палитры,
   выбор/claim серий для генерации, выбор/claim due reminders и lookup delivery
   по `(reminder_id, scheduled_for)`. Они остаются системными и не получают
   искусственный `user_id`.
5. **Транзакции:** см. 7.4. Никаких дополнительных commit/rollback.
6. **Производительность:** списки tasks/projects, unarchive и генерация
   сохраняют batch-запросы и bulk-flush; маппинг не делает запросов.
7. **Reminder delivery:** batch 100, soft deadline 240s, at-least-once,
   lease/backoff/идемпотентность, commit-порядок sent-before-slot.
8. **Generation:** горизонт 90 дней, `FOR UPDATE SKIP LOCKED`, идемпотентность
   по `(series_id, due_at)`, завершённые серии не выбираются.
9. **Lock order:** series before tasks во всех series-wide операциях.
10. **Flush-before-dedupe:** `create_from_task` сохраняет flush перед
    генерацией.
11. **Auth:** refresh rotation/reuse/grace, cookie-флаги, origin-guard,
    commit-on-error — без изменений.
12. **Admin:** immutable slug, last-active-color, FK conflict — без изменений.
13. **Логирование:** те же события/поля; raw token/init data не логируются.

## 9. Изменения по слоям

- **domain/** — новый пакет: `enums.py`, `entities.py`,
  `task_management/models.py`, `task_management/policies.py`,
  `auth/models.py`.
- **application/** — новый пакет: `ports/task_management.py`,
  `ports/collaborators.py`, `ports/auth.py`, `task_management/*`, `auth/service.py`.
- **infrastructure/** — новый пакет: `persistence/sqlalchemy/*`,
  `telegram/sender.py`.
- **api/** — `mappers/*`, `queries/*`, `auth/cookies.py`, тонкие роуты,
  чистые Pydantic-схемы, `deps.py` через composition.
- **composition/** — новый пакет с фабриками.
- **models/** — импортирует enum'ы из `domain.enums`; остаётся ORM.
- **tasks/** — generation/delivery используют composition; телеграм-отправка
  импортирует infrastructure sender.
- **admin/** — palette policy через `PaletteService`; остальные views без
  изменений.
- **auth/** — старые модули становятся re-export-шимами. Реальные модули:
  `application/auth/{service,errors}.py`, `domain/auth/{models,tokens}.py`,
  `infrastructure/auth/{init_data,tokens}.py`,
  `infrastructure/persistence/sqlalchemy/auth_repository.py`,
  `api/auth/{cookies,schemas}.py`.
- **tests/** — domain-фабрики, contract/architecture/mapper-тесты, обновление
  repo SQL-тестов на новые адаптеры, сохранение поведенческих тестов.

## 10. Тестовая стратегия

- TDD: каждая задача начинается с падающего теста.
- **Architecture tests:** AST-обход `src/backend/domain` и
  `src/backend/application`; запрещённые импорты из раздела 4. Отдельно —
  запрет ORM-импортов в `api/task_schemas.py` и `api/routes/*`.
- **Domain tests:** поля сущностей совпадают с persisted-полями ORM; DTO
  собираются без SQLAlchemy; enum re-export работает.
- **Mapper tests:** round-trip ORM <-> domain для каждой сущности; defaults,
  `None`, UUID, timezone-aware datetime.
- **Port contract tests:** fake и SQLAlchemy-адаптеры имеют все методы
  протоколов; сигнатуры совместимы.
- **Repository SQL tests:** существующие compile-тесты переводятся на
  агрегатные адаптеры; проверяются `WHERE user_id`, `IN`, `LIMIT`,
  `FOR UPDATE`, `SKIP LOCKED`, `ORDER BY`; новые `save_*` дают `UPDATE`/
  `INSERT`.
- **Service tests:** существующие `test_task_management_*` переводятся на
  domain-сущности; assertions поведения не меняются.
- **API tests:** существующие `test_*_api.py` и `test_cross_user_access.py`
  сохраняются; response JSON и статусы не меняются; добавляются mapper-тесты.
- **OpenAPI contract test:** фиксирует пути, методы, operation IDs,
  query-параметры, response schema names и документированные error-статусы;
  сравнение до/после рефакторинга.
- **Transaction tests:** `test_deps.py` (request commit/rollback),
  generation commit/rollback, dispatcher commit-order, auth commit-on-error.
- **Admin tests:** `test_admin_views.py`/`test_admin.py` плюс проверка, что
  palette policy вызывается через сервис.
- Тесты не требуют PostgreSQL, NATS или Redis.

## 11. Совместимость и миграция

- Рефакторинг выполняется по задачам плана; каждая задача — отдельный commit
  и зелёный полный прогон.
- Старые импорты продолжают работать через re-export-шимы; новые внутренние
  модули используют только новые пути.
- Никаких изменений API/БД/поведения; при откате одной задачи шимы позволяют
  вернуться к предыдущему состоянию без миграции данных.
- Высокорисковые зоны и меры:
  - **Reminder dispatch / generation:** сохранить commit-точки, candidate
    snapshots, flush-before-dedupe; покрыть отдельными тестами до миграции.
  - **Auth refresh:** commit-on-error через `UnitOfWork`; init-data/JWT
    адаптеры возвращают те же ошибки и claims; тесты reuse/grace/expiry.
  - **Tenant scoping:** каждый tenant-scoped адаптер сохраняет `user_id` в SQL;
    compile-тесты и `test_cross_user_access.py`; системные запросы из 8.4
    проверяются отдельно.
  - **Admin palette:** поведение last-active-color/slug/FK; тесты admin.
  - **Performance:** batch-запросы; тесты на количество запросов/`IN`.

## 12. Проверка результата

1. `poetry run pytest -q` — все тесты зелёные.
2. `poetry run ruff check .` — без ошибок.
3. `poetry run ruff format --check src tests` — без изменений.
4. `poetry run mypy src tests` — без ошибок.
5. `poetry run pytest --cov=backend --cov-report=term-missing` — покрытие не
   падает; новые ветки мапперов/портов покрыты.
6. Архитектурный тест подтверждает: `domain`/`application` не импортируют
   внешние слои; API не импортирует ORM.
7. OpenAPI contract test и `tests/test_route_auth_matrix.py` подтверждают
   неизменность HTTP-контракта.
8. `graphify update .` — граф обновлён.
9. `git diff` не содержит изменений схемы БД, Alembic-миграций, middleware
   order, Taskiq schedules, cookie-флагов и публичных текстов ошибок.
