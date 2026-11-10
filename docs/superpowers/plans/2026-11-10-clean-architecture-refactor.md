# Clean Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the backend to a dependency-inward architecture (`domain <- application <- infrastructure/api/tasks/admin`) while preserving every HTTP contract, database schema, transaction boundary, and business behavior.

**Architecture:** Strangler migration. New `domain`, `application`, `infrastructure`, and `composition` packages are introduced alongside the current modules. Old modules keep working and become thin re-export shims at the end. Each task is a separate commit with a green full test suite.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL, Taskiq, pytest, mypy, ruff.

**Spec:** `docs/superpowers/specs/2026-11-10-clean-architecture-refactor-design.md`

## Global Constraints

- No new runtime or dev dependencies.
- No database migration: no table, column, index, constraint, or seed changes.
- HTTP contracts are frozen: paths, methods, status codes, JSON bodies, error envelope, `request_id`, cursor format, OpenAPI operation IDs, and documented error statuses.
- Business rules are frozen: recurrence, reminder slots, pagination, soft/hard delete, archive/unarchive, tenancy, limits, and Russian error descriptions.
- Transaction boundaries are frozen: request commit in `Database.session()`, per-series commit/rollback in `SeriesService.generate`, dispatcher commit points, auth commit-on-error, series-before-task lock order, and flush-before-dedupe in `create_from_task`.
- Domain and application modules must not import FastAPI, Starlette, SQLAlchemy, Taskiq, `backend.models`, `backend.db`, `backend.infrastructure`, `backend.api`, `backend.tasks`, `backend.admin`, or `backend.composition`.
- Concrete adapters are chosen only in `backend/composition`.
- Service migration tasks (Tasks 8-13) create and test new application modules while the old modules remain in place. Task 14 switches the API/worker wiring to the composition root and only then replaces every old module with a re-export shim. No old module is replaced before the wiring is switched.
- Every new or changed function/class gets an English docstring in the project template; stale docstrings are updated in the same change.
- During migration, test doubles and factories may use `Any`/`cast` to bridge ORM and domain types; Task 18 removes those bridges.
- Verify after every task: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`.
- After the final task: `graphify update .` and `git status --short` must show only intended changes.
- Do not stage unrelated pre-existing changes.

## Review Focus

- Reminder dispatch must keep the exact commit order: a claim is committed only when the candidate is skipped, a successful send commits after `mark_delivery_sending`, after `mark_delivery_sent`, and after the slot update, other outcomes commit after handling, and unexpected errors roll back; batch 100 and soft deadline 240 s.
- Series generation must keep per-series commit/rollback, candidate-id snapshotting, `FOR UPDATE SKIP LOCKED`, and idempotency by `(series_id, due_at)`.
- Auth refresh must keep reuse/grace/expiry behavior and commit revocation before raising `401`.
- Tenant isolation must stay in every adapter query: cross-user reads return `None`/404 and mutations are no-ops.
- API responses, cursor behavior, error envelope, and OpenAPI operation IDs must be byte-compatible from the client's perspective.

---

### Task 0: Freeze the baseline API contract

**Files:**
- Create: `tests/test_openapi_contract.py`
- Test: `tests/test_openapi_contract.py`

**Interfaces:**
- Consumes: the current `create_app(settings)` and its generated OpenAPI schema.
- Produces: literal expected sets of paths, methods, operation IDs, and documented error statuses used by Task 18.

- [ ] **Step 1: Write the baseline contract test**

Create a test that builds the current app and compares its OpenAPI schema with literals captured from the current output:

```python
from backend.core.config import Settings
from backend.main import create_app


EXPECTED_PATHS = {"/api/v1/tasks", "/api/v1/projects", ...}
EXPECTED_OPERATION_IDS = {"get /api/v1/tasks": "tasks-list_tasks", ...}
EXPECTED_ERROR_STATUSES = {"get /api/v1/tasks": {"400", "401", "422", "500", "503"}, ...}


def test_openapi_surface_is_frozen() -> None:
    """The public route surface is frozen before the refactor starts."""
    app = create_app(Settings(_env_file=None, app_env="local", log_json=False))
    spec = app.openapi()
    assert set(spec["paths"]) == EXPECTED_PATHS
    actual_ids = {
        f"{method} {path}": operation["operationId"]
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if method in {"get", "post", "patch", "delete", "put"}
    }
    assert actual_ids == EXPECTED_OPERATION_IDS
    for key, statuses in EXPECTED_ERROR_STATUSES.items():
        method, path = key.split(" ", 1)
        assert set(spec["paths"][path][method]["responses"]) >= statuses
```

- [ ] **Step 2: Run the test against the current application**

Run: `poetry run pytest tests/test_openapi_contract.py -q`
Expected: PASS after filling the literals from the current OpenAPI output. This commit is the pre-refactor baseline.

- [ ] **Step 3: Commit the baseline**

```bash
git add tests/test_openapi_contract.py
git commit -m "test: freeze baseline OpenAPI contract"
```

---

### Task 1: Domain foundation and architecture guard

**Files:**
- Create: `src/backend/domain/__init__.py`
- Create: `src/backend/domain/enums.py`
- Create: `src/backend/domain/entities.py`
- Create: `src/backend/domain/task_management/__init__.py`
- Create: `src/backend/domain/task_management/models.py`
- Create: `src/backend/domain/task_management/policies.py`
- Create: `src/backend/domain/auth/__init__.py`
- Create: `src/backend/domain/auth/models.py`
- Create: `src/backend/domain/auth/tokens.py`
- Modify: `src/backend/task_management/enums.py` (re-export)
- Modify: `src/backend/models/task.py`, `task_series.py`, `task_reminder.py`, `reminder_delivery.py` (import enums from `backend.domain.enums`)
- Test: `tests/test_domain_entities.py`
- Test: `tests/test_architecture.py`

**Interfaces:**
- Produces: `Priority`, `SeriesFrequency`, `TaskRecurrence`, `ReminderKind`, `DeliveryStatus` in `backend.domain.enums`.
- Produces: `User`, `TelegramProfile`, `AuthSession`, `ColorPalette`, `Label`, `Project`, `TaskSeries`, `Task`, `TaskReminder`, `ReminderDelivery`, `TaskLabel`, `SeriesLabel` as mutable dataclasses in `backend.domain.entities`.
- Produces: `TaskFilters`, `TaskDetail`, `TaskListCursor`, `TaskPage`, `ProjectProgress`, `ProjectSummary`, `ProjectListCursor`, `ProjectPage`, `SeriesDetail`, `ReminderInput`, `ReminderContent`, `UNSET`, `UnsetType` in `backend.domain.task_management.models`.
- Produces: pure date/schedule/reminder functions and constants (`require_supported_date`, `require_supported_datetime`, `localize_wall_time`, `next_occurrence_after_date`, `series_due_times`, `first_scheduled_time`, `first_phase_scheduled_time`, `before_due_time`, `build_reminder_text`, `build_task_reminder`, `build_series_reminder`, `reminder_input_from_row`, `rebuild_reminder_slot`, `next_reminder_slot`, `validate_reminder_inputs`, `HORIZON_DAYS`) in `backend.domain.task_management.policies`.
- Produces: `AccessTokenClaims`, `TokenPair`, `LoginResult`, `TelegramUser`, `TelegramIdentity`, `AuthSettings` in `backend.domain.auth.models`.
- Produces: `generate_refresh_token`, `hash_refresh_token` in `backend.domain.auth.tokens`.
- Consumes: existing ORM mapped columns as the field-parity source of truth.

- [ ] **Step 1: Write the failing domain tests**

`tests/test_domain_entities.py` must assert field parity between domain dataclasses and ORM models:

```python
from dataclasses import fields
from sqlalchemy import inspect

from backend.domain.entities import (
    AuthSession,
    ColorPalette,
    Label,
    Project,
    ReminderDelivery,
    Task,
    TaskReminder,
    TaskSeries,
    TelegramProfile,
    User,
)
from backend.models.auth_session import AuthSession as AuthSessionRow
from backend.models.color_palette import ColorPalette as ColorPaletteRow
from backend.models.label import Label as LabelRow
from backend.models.project import Project as ProjectRow
from backend.models.reminder_delivery import ReminderDelivery as ReminderDeliveryRow
from backend.models.task import Task as TaskRow
from backend.models.task_reminder import TaskReminder as TaskReminderRow
from backend.models.task_series import TaskSeries as TaskSeriesRow
from backend.models.telegram_profile import TelegramProfile as TelegramProfileRow
from backend.models.user import User as UserRow

import pytest


@pytest.mark.parametrize(
    ("entity", "row"),
    [
        (User, UserRow),
        (TelegramProfile, TelegramProfileRow),
        (AuthSession, AuthSessionRow),
        (ColorPalette, ColorPaletteRow),
        (Label, LabelRow),
        (Project, ProjectRow),
        (TaskSeries, TaskSeriesRow),
        (Task, TaskRow),
        (TaskReminder, TaskReminderRow),
        (ReminderDelivery, ReminderDeliveryRow),
    ],
)
def test_domain_entity_has_every_orm_column(entity: type, row: type) -> None:
    """Every persisted column has a matching domain field."""
    domain_fields = {item.name for item in fields(entity)}
    column_fields = {column.key for column in inspect(row).columns}
    assert column_fields <= domain_fields
```

`tests/test_architecture.py` must assert the dependency rule with an AST scan:

```python
from pathlib import Path
import ast


FORBIDDEN = (
    "fastapi",
    "starlette",
    "sqlalchemy",
    "taskiq",
    "pydantic",
    "pydantic_settings",
    "jwt",
    "backend.models",
    "backend.db",
    "backend.infrastructure",
    "backend.api",
    "backend.tasks",
    "backend.admin",
    "backend.composition",
    "backend.core.config",
    "backend.core.middleware",
    "backend.task_management",
    "backend.auth",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_domain_and_application_do_not_import_outer_layers() -> None:
    """Inner layers never depend on frameworks, adapters, or the composition root."""
    roots = [Path("src/backend/domain"), Path("src/backend/application")]
    for root in roots:
        for path in root.rglob("*.py"):
            for imported in _imports(path):
                assert not imported.startswith(FORBIDDEN), f"{path}: {imported}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_domain_entities.py tests/test_architecture.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.domain'`.

- [ ] **Step 3: Implement the domain foundation**

Create the dataclasses with the fields listed in spec section 7.1. Use mutable `@dataclass`, `id: int = 0`, `created_at`/`updated_at` with `field(default_factory=lambda: datetime.now(UTC))`, and no SQLAlchemy imports. Reorder fields so required fields precede defaulted ones; the parity test checks names, not order. Move the enum definitions into `backend.domain.enums`; replace `backend/task_management/enums.py` with a re-export shim:

```python
from backend.domain.enums import DeliveryStatus, Priority, ReminderKind, SeriesFrequency, TaskRecurrence

__all__ = ["DeliveryStatus", "Priority", "ReminderKind", "SeriesFrequency", "TaskRecurrence"]
```

Copy the frozen DTOs from `task_management.tasks/projects/series/reminders` into `domain/task_management/models.py` without changing fields; leave the old definitions in place until their owning service is migrated in Tasks 8-13. Copy the pure `date_bounds`, `schedule`, and reminder rendering/slot/validation/builder functions (including `build_task_reminder`, `build_series_reminder`, `reminder_input_from_row`, and `HORIZON_DAYS`) into `domain/task_management/policies.py`; they must import only stdlib, `backend.core.errors`, and domain models. Add `domain/auth/models.py` with `AccessTokenClaims`, `TokenPair`, `LoginResult`, `TelegramUser`, `TelegramIdentity`, and a plain `AuthSettings` dataclass; add `domain/auth/tokens.py` with pure refresh-token helpers. Update the four ORM modules to import enums from `backend.domain.enums`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_domain_entities.py tests/test_architecture.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/domain src/backend/models/task.py src/backend/models/task_series.py src/backend/models/task_reminder.py src/backend/models/reminder_delivery.py src/backend/task_management/enums.py tests/test_domain_entities.py tests/test_architecture.py
git commit -m "refactor: add ORM-free domain foundation"
```

---

### Task 2: Application ports and UnitOfWork

**Files:**
- Create: `src/backend/application/__init__.py`
- Create: `src/backend/application/ports/__init__.py`
- Create: `src/backend/application/ports/unit_of_work.py`
- Create: `src/backend/application/ports/task_management.py`
- Create: `src/backend/application/ports/collaborators.py`
- Create: `src/backend/application/ports/auth.py`
- Create: `src/backend/application/ports/legacy.py`
- Test: `tests/test_port_contracts.py`

**Interfaces:**
- Produces: `UnitOfWork` in `backend.application.ports.unit_of_work`; `task_management` and `auth` re-export it.
- Produces: `PaletteRepository`, `AccountRepository`, `ProjectRepository`, `LabelRepository`, `TaskRepository`, `SeriesRepository`, `ReminderRepository` in `backend.application.ports.task_management`.
- Produces: `ProjectReader`, `LabelReader`, `TaskReader`, `SeriesReader`, `SeriesOperations`, `PalettePolicy`, `ReminderSender` in `backend.application.ports.collaborators`.
- Produces: `AuthRepository`, `InitDataValidator`, `TokenIssuer` in `backend.application.ports.auth`.
- Produces: `TaskManagementRepository` composite compatibility facade in `backend.application.ports.legacy`.
- Consumes: domain entities and DTOs from Task 1.

- [ ] **Step 1: Write the failing port contract test**

Assert that each protocol declares the method names from spec section 7.3:

```python
from backend.application.ports import task_management as ports


def _methods(protocol: type) -> set[str]:
    return {name for name in dir(protocol) if not name.startswith("_")}


def test_aggregate_protocols_expose_expected_methods() -> None:
    """The fat repository is split into aggregate-specific contracts."""
    assert "list_palette_colors" in _methods(ports.PaletteRepository)
    assert "save_project" in _methods(ports.ProjectRepository)
    assert "save_label" in _methods(ports.LabelRepository)
    assert "save_task" in _methods(ports.TaskRepository)
    assert "claim_series_for_generation" in _methods(ports.SeriesRepository)
    assert "claim_due_reminder" in _methods(ports.ReminderRepository)
    assert _methods(ports.UnitOfWork) >= {"flush", "commit", "rollback"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `poetry run pytest tests/test_port_contracts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.application'`.

- [ ] **Step 3: Implement the protocols**

Copy the existing method names and parameters from `task_management/ports.py` into the aggregate protocols, replacing ORM types with domain entities. Add `save_*` methods:

```python
async def save_project(self, project: Project) -> None: ...
async def save_label(self, label: Label) -> None: ...
async def save_task(self, task: Task) -> None: ...
async def save_series(self, series: TaskSeries) -> None: ...
async def save_reminder(self, reminder: TaskReminder) -> None: ...
async def save_delivery(self, delivery: ReminderDelivery) -> None: ...
```

Define `UnitOfWork` in `application/ports/unit_of_work.py` and re-export it from the task-management and auth port modules. Add the narrow collaborator protocols and `AuthRepository`/`InitDataValidator`/`TokenIssuer` using domain auth entities and scalars. Add `application/ports/legacy.py` with the composite `TaskManagementRepository` Protocol; it is the only compatibility facade. Add `@runtime_checkable` only where a test needs `isinstance`; otherwise keep plain `Protocol`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `poetry run pytest tests/test_port_contracts.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application tests/test_port_contracts.py
git commit -m "refactor: add aggregate application ports and unit of work"
```

---

### Task 3: SQLAlchemy mappers and UnitOfWork adapter

**Files:**
- Create: `src/backend/infrastructure/__init__.py`
- Create: `src/backend/infrastructure/persistence/__init__.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/__init__.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/mappers.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/unit_of_work.py`
- Test: `tests/test_sqlalchemy_mappers.py`
- Test: `tests/test_unit_of_work.py`

**Interfaces:**
- Produces: `user_to_entity`, `apply_user`, `profile_to_entity`, `apply_profile`, `auth_session_to_entity`, `apply_auth_session`, `color_to_entity`, `apply_color`, `label_to_entity`, `apply_label`, `project_to_entity`, `apply_project`, `series_to_entity`, `apply_series`, `task_to_entity`, `apply_task`, `reminder_to_entity`, `apply_reminder`, `delivery_to_entity`, `apply_delivery`, `task_label_to_entity`, `series_label_to_entity`.
- Produces: `SqlAlchemyUnitOfWork(session: AsyncSession)` implementing `UnitOfWork`.

- [ ] **Step 1: Write the failing mapper round-trip test**

```python
async def test_task_mapper_round_trip_preserves_persisted_fields() -> None:
    """Domain and ORM task representations round-trip without data loss."""
    row = TaskRow(
        id=1,
        user_id=7,
        series_id=3,
        project_id=2,
        title="Write",
        description="Body",
        priority="urgent_important",
        due_at=NOW,
        completed_at=None,
        reminders_enabled=True,
        deleted_at=None,
        deletion_batch_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    entity = task_to_entity(row)
    assert entity.id == 1
    assert entity.user_id == 7
    assert entity.series_id == 3
    assert entity.priority == "urgent_important"

    updated = replace(entity, title="Updated", completed_at=NOW)
    apply_task(updated, row)
    assert row.title == "Updated"
    assert row.completed_at == NOW
```

Add analogous tests for project, label, series, reminder, delivery, and auth entities. Add a unit-of-work test with a recording session:

```python
class RecordingSession:
    """Minimal session double recording transaction calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def flush(self) -> None:
        self.calls.append("flush")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


async def test_unit_of_work_delegates_flush_commit_rollback() -> None:
    """The adapter delegates transaction calls to the session."""
    session = RecordingSession()
    uow = SqlAlchemyUnitOfWork(session)
    await uow.flush()
    await uow.commit()
    await uow.rollback()
    assert session.calls == ["flush", "commit", "rollback"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_sqlalchemy_mappers.py tests/test_unit_of_work.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.infrastructure'`.

- [ ] **Step 3: Implement mappers and the UnitOfWork adapter**

Implement explicit field-by-field mapping. `apply_*` must copy only persisted columns and must not touch relationships. `SqlAlchemyUnitOfWork` must call `session.flush()`, `session.commit()`, and `session.rollback()` directly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_sqlalchemy_mappers.py tests/test_unit_of_work.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/infrastructure tests/test_sqlalchemy_mappers.py tests/test_unit_of_work.py
git commit -m "refactor: add ORM-domain mappers and unit of work adapter"
```

---

### Task 4: Palette, account, and project SQLAlchemy adapters

**Files:**
- Create: `src/backend/infrastructure/persistence/sqlalchemy/palette_repository.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/account_repository.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/project_repository.py`
- Test: `tests/test_task_management_repository.py` (translate palette/account/project SQL tests to the new adapters; keep SQL assertions, replace ORM identity assertions with domain-return/mapper assertions)
- Test: `tests/test_cross_user_access.py` (unchanged behavior)

**Interfaces:**
- Produces: `SqlAlchemyPaletteRepository(session)`, `SqlAlchemyAccountRepository(session)`, `SqlAlchemyProjectRepository(session)`.
- Consumes: `PaletteRepository`, `AccountRepository`, `ProjectRepository`, domain entities, `mappers`.

- [ ] **Step 1: Write failing adapter tests**

Add tests that instantiate the new adapters with `RecordingSession` and assert domain return types and tenant filtering:

```python
async def test_project_repository_returns_domain_and_scopes_tenant() -> None:
    """Project reads return domain entities and always filter by user."""
    session = RecordingSession(results=[RecordingResult(values=[])])
    repository = SqlAlchemyProjectRepository(session)
    projects = await repository.list_projects(
        USER_ID,
        include_archived=False,
        limit=50,
        after_archived=None,
        after_name=None,
        after_project_id=None,
    )
    assert projects == []
    sql = compile_statement(session.statements[-1])
    assert "projects.user_id = 7" in sql
    assert "lower(projects.name) AS name_key" in sql
```

Add `test_palette_repository_returns_domain_colors`, `test_account_repository_reads_timezone_and_telegram_id`, and `test_project_save_issues_update`. Preserve the existing `list_projects` contract: it returns `Sequence[tuple[Project, name_key]]` and the SQL still computes `lower(name) AS name_key`; assert that in a compile test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_repository.py -q -k 'palette_repository or account_repository or project_repository'`
Expected: FAIL with import errors for the new modules.

- [ ] **Step 3: Implement the adapters**

Move the corresponding methods from `SqlAlchemyTaskManagementRepository` into the new classes, replacing ORM returns with `to_entity` mappers and adding `save_project`. Keep every `user_id` predicate, `ORDER BY`, `LIMIT`, lock, flush point, and the `list_projects` tuple/`name_key` shape unchanged. Add per-adapter test factories (`make_palette_repository`, `make_account_repository`, `make_project_repository`, etc.) alongside the existing `make_repository`, so the translated tests exercise the new adapters while the not-yet-translated label/task/series/reminder tests keep using the old helper. Retire `make_repository` in Task 6 after all repository tests are translated. Do not change the old repository yet.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_repository.py tests/test_cross_user_access.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/infrastructure/persistence/sqlalchemy/palette_repository.py src/backend/infrastructure/persistence/sqlalchemy/account_repository.py src/backend/infrastructure/persistence/sqlalchemy/project_repository.py tests/test_task_management_repository.py
git commit -m "refactor: split palette, account, and project persistence adapters"
```

---

### Task 5: Label and task SQLAlchemy adapters

**Files:**
- Create: `src/backend/infrastructure/persistence/sqlalchemy/label_repository.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/task_repository.py`
- Test: `tests/test_task_management_repository.py` (translate label/task SQL tests to the new adapters; keep SQL assertions, replace ORM identity assertions with domain-return/mapper assertions)
- Test: `tests/test_cross_user_access.py`

**Interfaces:**
- Produces: `SqlAlchemyLabelRepository(session)`, `SqlAlchemyTaskRepository(session)`.
- Consumes: `LabelRepository`, `TaskRepository`, domain entities, mappers.

- [ ] **Step 1: Write failing adapter tests**

Add tests for label CRUD/domain returns, task list cursor/limit/tenant scoping, batch label/reminder loaders, and `save_task`:

```python
async def test_task_repository_list_keeps_cursor_and_tenant_scope() -> None:
    """Task listing preserves ordering, limit, and user scope."""
    session = RecordingSession(results=[RecordingResult(values=[])])
    repository = SqlAlchemyTaskRepository(session)
    tasks = await repository.list_tasks(
        USER_ID,
        date_start=None,
        date_end=None,
        project_ids=(),
        priorities=(),
        status="all",
        recurrence="all",
        query=None,
        include_archived_projects=False,
        now=NOW,
        limit=51,
        after_due_at=NOW,
        after_task_id=9,
    )
    assert tasks == []
    sql = compile_statement(session.statements[-1])
    assert "tasks.user_id = 7" in sql
    assert "ORDER BY tasks.due_at, tasks.id" in sql
    assert "LIMIT 51" in sql
```

Add `test_task_save_issues_update`, `test_batch_task_loaders_use_in_and_user_scope`, and `test_label_save_issues_update`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_repository.py -q -k 'task_repository or label_repository'`
Expected: FAIL with import errors.

- [ ] **Step 3: Implement the adapters**

Move the corresponding methods from the old repository. Keep batch `IN` queries, `LIMIT limit + 1` semantics, status/recurrence predicates, soft/hard delete, and `flush` points. Add `save_task` and `save_label` using `apply_*` + `flush`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_repository.py tests/test_cross_user_access.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/infrastructure/persistence/sqlalchemy/label_repository.py src/backend/infrastructure/persistence/sqlalchemy/task_repository.py tests/test_task_management_repository.py
git commit -m "refactor: split label and task persistence adapters"
```

---

### Task 6: Series and reminder SQLAlchemy adapters

**Files:**
- Create: `src/backend/infrastructure/persistence/sqlalchemy/series_repository.py`
- Create: `src/backend/infrastructure/persistence/sqlalchemy/reminder_repository.py`
- Test: `tests/test_task_management_repository.py` (translate series/reminder SQL tests to the new adapters; keep SQL assertions, replace ORM identity assertions with domain-return/mapper assertions)
- Test: `tests/test_task_management_reminders.py`
- Test: `tests/test_cross_user_access.py`

**Interfaces:**
- Produces: `SqlAlchemySeriesRepository(session)`, `SqlAlchemyReminderRepository(session)`.
- Consumes: `SeriesRepository`, `ReminderRepository`, domain entities, mappers.

- [ ] **Step 1: Write failing adapter tests**

Cover generation selection/locking, series task locks, due-slot lookup, reminder dispatch claims, delivery state transitions, and tenant scoping:

```python
async def test_series_repository_claim_uses_skip_locked() -> None:
    """Series generation claims remain concurrency-safe."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = SqlAlchemySeriesRepository(session)
    await repository.claim_series_for_generation(3, USER_ID)
    sql = compile_statement(session.statements[-1])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "task_series.user_id = 7" in sql
```

Add `test_series_due_slot_exists_is_owner_scoped`, `test_reminder_repository_claim_due_reminder_uses_lock`, and `test_delivery_state_transitions_are_persisted`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_repository.py tests/test_task_management_reminders.py -q -k 'series_repository or reminder_repository'`
Expected: FAIL with import errors.

- [ ] **Step 3: Implement the adapters**

Move the methods from the old repository without changing SQL. Reminder command methods (`mark_delivery_sent`, `cancel_delivery`, etc.) must keep their exact commit-free, flush-only behavior. Add `save_series`, `save_reminder`, and `save_delivery`. After all repository tests are translated, remove the old `make_repository` helper.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_repository.py tests/test_task_management_reminders.py tests/test_cross_user_access.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/infrastructure/persistence/sqlalchemy/series_repository.py src/backend/infrastructure/persistence/sqlalchemy/reminder_repository.py tests/test_task_management_repository.py tests/test_task_management_reminders.py
git commit -m "refactor: split series and reminder persistence adapters"
```

---

### Task 7: Auth SQLAlchemy adapter

**Files:**
- Create: `src/backend/infrastructure/persistence/sqlalchemy/auth_repository.py`
- Test: `tests/test_auth_repository.py`
- Test: `tests/test_auth_service.py` (unchanged behavior)

**Interfaces:**
- Produces: `SqlAlchemyAuthRepository(session)` implementing the final `AuthRepository` contract:
  `get_identity_by_telegram_id(telegram_id) -> TelegramIdentity | None`,
  `create_user_with_profile(data: TelegramUser) -> TelegramIdentity`,
  `update_profile(profile: TelegramProfile, data: TelegramUser) -> None`,
  plus the existing session/user methods returning domain entities.
- Consumes: auth domain entities, mappers.

- [ ] **Step 1: Write the failing adapter tests**

Keep the existing SQL-compile tests and add domain-identity assertions:

```python
async def test_auth_repository_returns_domain_identity() -> None:
    """Profile lookups return the domain user/profile pair, not ORM rows."""
    session = RecordingSession(results=[RecordingResult(values=[(UserRow(...), TelegramProfileRow(...))])])
    repository = SqlAlchemyAuthRepository(session)
    identity = await repository.get_identity_by_telegram_id(123)
    assert isinstance(identity, TelegramIdentity)
    assert identity.profile.telegram_id == 123
    assert identity.user.id == identity.profile.user_id
```

Add `test_get_session_for_update_compiles_for_update`, `test_revoke_family_updates_only_active_rows`, and `test_update_user_timezone_returns_domain_user`. Replace the old `revoke_*_and_commit` tests with assertions that the adapter exposes only `revoke_session`/`revoke_family` and never commits; commit-on-error is covered by the UnitOfWork test and Task 13.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_auth_repository.py -q -k 'domain_profile or sqlalchemy_auth'`
Expected: FAIL with import error for the new adapter.

- [ ] **Step 3: Implement the adapter**

Move the SQL from `backend/auth/repository.py` into `SqlAlchemyAuthRepository`, map results to domain entities, and remove commit calls. Implement the final identity-based login contract (`get_identity_by_telegram_id`, `create_user_with_profile`, `update_profile`) in one query for the profile+user pair, preserving the current query count. Keep advisory locks, `FOR UPDATE`, tenant/user scoping, and update semantics. The old `backend/auth/repository.py` remains unchanged until Task 14.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_auth_repository.py tests/test_auth_service.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/infrastructure/persistence/sqlalchemy/auth_repository.py tests/test_auth_repository.py
git commit -m "refactor: split auth persistence adapter"
```

---

### Task 8: Migrate PaletteService and LabelService

**Files:**
- Create: `src/backend/application/task_management/__init__.py`
- Create: `src/backend/application/task_management/palette.py`
- Create: `src/backend/application/task_management/labels.py`
- Modify: `tests/task_management_fakes.py` (domain colors/labels)
- Test: `tests/test_task_management_palette.py`
- Test: `tests/test_task_management_labels.py`
- Test: `tests/test_color_palette_api.py`
- Test: `tests/test_labels_api.py`
- Deferred to Task 14: `src/backend/task_management/palette.py` and `labels.py` remain unchanged and become shims only after wiring switches.

**Interfaces:**
- Consumes: `PaletteRepository`, `LabelRepository`, `PalettePolicy`.
- Produces: `PaletteService(repository: PaletteRepository)`, `LabelService(repository: LabelRepository, palette_service: PalettePolicy, clock: Callable[[], datetime] | None = None)`.
- Produces: `LabelService.create/update/delete` persist mutations via `add_label`/`save_label`/`soft_delete_label`.

- [ ] **Step 1: Write the failing service tests**

Add tests that assert domain types and explicit persistence:

```python
async def test_label_update_persists_mutated_domain_entity() -> None:
    """Label updates call save_label after mutating the domain entity."""
    repository = FakeTaskManagementRepository()
    label = repository.insert_label(make_label(name="Old"))
    service = LabelService(repository, PaletteService(repository), clock=fixed_now)

    updated = await service.update(USER_ID, label.id, name="New")

    assert updated.name == "New"
    assert repository.saved_labels == [label.id]
    assert repository.labels[label.id].name == "New"
```

Add `saved_labels`/`saved_projects` counters to the fake and assert them, so the tests fail if the service mutates the object without calling `save_label`/`save_project`. Add `test_palette_list_returns_domain_colors` and `test_label_delete_soft_deletes_domain_entity`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_palette.py tests/test_task_management_labels.py -q`
Expected: FAIL because services still import ORM models and the old fat port.

- [ ] **Step 3: Implement the services and shims**

Move the service code into `application/task_management/palette.py` and `labels.py`. Replace ORM imports with domain entities. Use `save_label` after mutations. Keep all error messages, normalization, and clock behavior. Do not modify the old service modules yet; they keep serving API wiring until Task 14. Update the fake repository's color/label collections and factories to domain entities; update test imports to the new application modules.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_palette.py tests/test_task_management_labels.py tests/test_color_palette_api.py tests/test_labels_api.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application/task_management/palette.py src/backend/application/task_management/labels.py tests/task_management_fakes.py tests/test_task_management_palette.py tests/test_task_management_labels.py tests/test_color_palette_api.py tests/test_labels_api.py
git commit -m "refactor: move palette and label services to application layer"
```

---

### Task 9: Migrate ProjectService

**Files:**
- Create: `src/backend/application/task_management/projects.py`
- Modify: `tests/task_management_fakes.py` (domain projects)
- Test: `tests/test_task_management_projects.py`
- Test: `tests/test_projects_api.py`
- Test: `tests/test_task_management_integration.py`
- Deferred to Task 14: `src/backend/task_management/projects.py` remains unchanged and becomes a shim only after wiring switches.

**Interfaces:**
- Consumes: `ProjectRepository`, `PalettePolicy`, `ReminderRepository`.
- Produces: `ProjectService(repository: ProjectRepository, palette_service: PalettePolicy, reminder_repository: ReminderRepository, clock: Callable[[], datetime] | None = None)`.
- Produces: `ProjectService.create/update/archive/unarchive/delete` persist mutations via `add_project`/`save_project`/`soft_delete_project`.

- [ ] **Step 1: Write the failing service tests**

Add `test_project_update_persists_mutated_domain_entity` and `test_project_archive_delegates_reminder_pause`:

```python
async def test_project_update_persists_mutated_domain_entity() -> None:
    """Project updates call save_project after mutating the domain entity."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(name="Old"))
    service = ProjectService(
        repository,
        PaletteService(repository),
        repository,
        clock=fixed_now,
    )

    updated = await service.update(USER_ID, project.id, name="New")

    assert updated.name == "New"
    assert repository.saved_projects == [project.id]
    assert repository.projects[project.id].name == "New"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_projects.py -q`
Expected: FAIL because the service still imports ORM models and the fat port.

- [ ] **Step 3: Implement the service and shim**

Move the code to `application/task_management/projects.py`, replace ORM types with domain entities, and call `save_project` after mutations. Keep progress/color batch behavior, name rules, archive/unarchive reminder delegation, and error messages. Do not modify the old service module yet; it keeps serving API wiring until Task 14. Update the fake and tests to domain projects and the new application module, and make the fake record `save_project` calls in `saved_projects`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_projects.py tests/test_projects_api.py tests/test_task_management_integration.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application/task_management/projects.py tests/task_management_fakes.py tests/test_task_management_projects.py tests/test_projects_api.py tests/test_task_management_integration.py
git commit -m "refactor: move project service to application layer"
```

---

### Task 10: Migrate SeriesService

**Files:**
- Create: `src/backend/application/task_management/series.py`
- Modify: `tests/task_management_fakes.py` (domain series/tasks)
- Test: `tests/test_task_management_series.py`
- Test: `tests/test_task_series_api.py`
- Test: `tests/test_task_management_integration.py`
- Test: `tests/test_task_generation.py`
- Deferred to Task 14: `src/backend/task_management/series.py` remains unchanged and becomes a shim only after wiring switches.

**Interfaces:**
- Consumes: `SeriesRepository`, `TaskRepository`, `ReminderRepository`, `AccountRepository`, `ProjectReader`, `LabelReader`, `UnitOfWork`.
- Produces: `SeriesService(repository: SeriesRepository, task_repository: TaskRepository, reminder_repository: ReminderRepository, account_repository: AccountRepository, project_reader: ProjectReader, label_reader: LabelReader, unit_of_work: UnitOfWork, clock: Callable[[], datetime] | None = None)`.
- Produces: `SeriesService.create/update/delete/generate/create_from_task/detach_task` with the existing behavior.

- [ ] **Step 1: Write the failing transaction tests**

Add tests that assert generation still commits per series and preserves flush-before-dedupe:

```python
async def test_generate_commits_each_series_and_rolls_back_failures() -> None:
    """Generation keeps per-series commit boundaries."""
    repository = FakeTaskManagementRepository()
    service = make_series_service(repository)
    repository.series_errors[2] = RuntimeError("boom")

    await service.generate(fixed_now())

    assert repository.commits >= 2
    assert repository.rollbacks == 1


async def test_create_from_task_flushes_before_generation() -> None:
    """The new series link is saved and visible to due-slot dedupe."""
    repository = FakeTaskManagementRepository()
    service = make_series_service(repository)
    task = repository.insert_task(make_task(user_id=USER_ID))
    await service.create_from_task(USER_ID, task, frequency=SeriesFrequency.DAILY)
    assert task.id in repository.saved_tasks
    assert repository.flush_calls >= 1
```

Add `saved_tasks`, `saved_series`, `saved_reminders`, and `flush_calls` counters to the fake so the tests prove explicit saves happened, not just object mutation.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_series.py -q -k 'generate or create_from_task'`
Expected: FAIL because the service still uses the fat port and ORM entities.

- [ ] **Step 3: Implement the service and shim**

Move the code to `application/task_management/series.py`. Replace `repository.commit/rollback` with `unit_of_work.commit/rollback`, keep `flush` before generation, and preserve the 90-day horizon, `FOR UPDATE SKIP LOCKED`, and bulk writes. Replace ORM types with domain entities and make every mutation explicit: call `save_task` before the dedupe `flush` in `create_from_task` and after `detach_task`; call `save_series` after series field mutations; call `save_reminder` for template reminder edits; call `save_task` for each mutated future instance. Do not modify the old service module yet; it keeps serving API/worker wiring until Task 14. Update fake series/tasks and tests to the new application module.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_series.py tests/test_task_series_api.py tests/test_task_management_integration.py tests/test_task_generation.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application/task_management/series.py tests/task_management_fakes.py tests/test_task_management_series.py tests/test_task_series_api.py tests/test_task_management_integration.py tests/test_task_generation.py
git commit -m "refactor: move series service to application layer"
```

---

### Task 11: Migrate TaskService

**Files:**
- Create: `src/backend/application/task_management/tasks.py`
- Modify: `tests/task_management_fakes.py` (domain tasks/reminders)
- Test: `tests/test_task_management_tasks.py`
- Test: `tests/test_tasks_api.py`
- Test: `tests/test_cross_user_access.py`
- Test: `tests/test_task_management_integration.py`
- Deferred to Task 14: `src/backend/task_management/tasks.py` remains unchanged and becomes a shim only after wiring switches.

**Interfaces:**
- Consumes: `TaskRepository`, `SeriesRepository`, `ReminderRepository`, `AccountRepository`, `ProjectReader`, `LabelReader`, `SeriesOperations`, `UnitOfWork`.
- Produces: `TaskService(repository: TaskRepository, series_repository: SeriesRepository, reminder_repository: ReminderRepository, account_repository: AccountRepository, project_reader: ProjectReader, label_reader: LabelReader, series_operations: SeriesOperations, unit_of_work: UnitOfWork, clock: Callable[[], datetime] | None = None)`.
- Produces: unchanged public task methods.

- [ ] **Step 1: Write the failing mutation and tenancy tests**

Add tests that every mutation persists through the new port and cross-user access stays 404:

```python
async def test_complete_persists_task_and_returns_domain_detail() -> None:
    """Completing a task saves the mutated domain entity."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(make_task(user_id=USER_ID))
    service = make_service(repository)

    detail = await service.complete(USER_ID, task.id)

    assert detail.task.completed_at == fixed_now()
    assert task.id in repository.saved_tasks
    assert repository.tasks[task.id].completed_at == fixed_now()


async def test_cross_user_task_update_does_not_mutate() -> None:
    """Another user's task is invisible and unchanged."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(make_task(user_id=USER_ID))
    service = make_service(repository)

    with pytest.raises(NotFoundError):
        await service.update(USER_ID + 1, task.id, title="Hacked")

    assert task.title == "Task"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py -q -k 'complete_persists or cross_user'`
Expected: FAIL because the service still uses the fat port and ORM entities.

- [ ] **Step 3: Implement the service and shim**

Move the code to `application/task_management/tasks.py`. Replace ORM imports with domain entities; replace the concrete `SeriesService` dependency with `SeriesOperations`; replace direct repository calls with the narrow ports; call `save_task` after every mutation. Preserve validation, cursor, batch detail loading, recurrence, reminder rebuild, delete delegation, and error messages. Do not modify the old service module yet; it keeps serving API wiring until Task 14. Update fake tasks/reminders and tests to the new application module, and make the fake record `save_task` calls in `saved_tasks`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_tasks.py tests/test_tasks_api.py tests/test_cross_user_access.py tests/test_task_management_integration.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application/task_management/tasks.py tests/task_management_fakes.py tests/test_task_management_tasks.py tests/test_tasks_api.py tests/test_cross_user_access.py tests/test_task_management_integration.py
git commit -m "refactor: move task service to application layer"
```

---

### Task 12: Migrate reminder policies and ReminderDispatcher

**Files:**
- Create: `src/backend/application/task_management/reminders.py`
- Modify: `tests/task_management_fakes.py` (domain reminders/deliveries)
- Test: `tests/test_task_management_reminders.py`
- Test: `tests/test_reminder_delivery.py`
- Test: `tests/test_task_management_integration.py`
- Deferred to Task 14: `src/backend/task_management/reminders.py`, `schedule.py`, and `date_bounds.py` remain unchanged and become shims only after wiring switches.

**Interfaces:**
- Consumes: `ReminderRepository`, `TaskReader`, `SeriesReader`, `ProjectReader`, `AccountRepository`, `UnitOfWork`, `ReminderSender`.
- Produces: `ReminderDispatcher(reminder_repository: ReminderRepository, task_reader: TaskReader, series_reader: SeriesReader, project_reader: ProjectReader, account_repository: AccountRepository, unit_of_work: UnitOfWork, sender: ReminderSender, clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] | None = None)`.
- Produces: `build_reminder_text`, `rebuild_reminder_slot`, `next_reminder_slot`, `validate_reminder_inputs` in `domain/task_management/policies.py`; `ReminderInput` and `ReminderContent` stay in `domain/task_management/models.py`.

- [ ] **Step 1: Write the failing dispatcher tests**

Keep the existing commit-order tests and add a test that dispatcher uses `UnitOfWork`, not repository commits:

```python
async def test_dispatch_commits_sending_sent_and_slot_update() -> None:
    """The dispatcher preserves its commit order through the UnitOfWork."""
    repository = FakeTaskManagementRepository()
    _, reminder = make_scheduled_reminder(repository)
    uow = RecordingUnitOfWork()
    dispatcher = ReminderDispatcher(
        repository,
        repository,  # task reader
        repository,  # series reader
        repository,  # project reader
        repository,  # account repository
        uow,
        FakeSender(),
        clock=fixed_now,
    )

    await dispatcher.dispatch_due()

    # Successful send: mark sending, mark sent, then update the next slot.
    # The claim branch commits only when a candidate is skipped.
    assert uow.calls == ["commit", "commit", "commit"]
    assert repository.deliveries[1].status == "sent"
```

Add a local `RecordingUnitOfWork` with a `calls` list in the test module; do not instantiate the reader Protocols directly. Keep the existing `test_sent_mark_is_committed_before_slot_update` to pin the sent-before-slot ordering.

Adapt the existing `test_sent_mark_is_committed_before_slot_update` and `test_unexpected_failure_keeps_earlier_delivery_and_continues` to the new constructor.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_reminders.py -q -k 'dispatch_commits or sent_mark'`
Expected: FAIL because the dispatcher still uses the fat port and repository commits.

- [ ] **Step 3: Implement the split and shims**

Use the pure `date_bounds`, `schedule`, and reminder functions already extracted to `domain/task_management/policies.py` in Task 1. Move `ReminderDispatcher` into `application/task_management/reminders.py`; replace ORM types with domain, replace `repository.commit/rollback` with `unit_of_work.commit/rollback`. Keep batch size, deadline, lease, backoff, idempotency, and commit order exactly. Do not modify the old modules yet; they keep serving API/worker wiring until Task 14. Update fake reminders/deliveries and tests to the new application/domain modules. Update `make_series_service` to pass the fake account repository and unit of work alongside the existing ports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_reminders.py tests/test_reminder_delivery.py tests/test_task_management_integration.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application/task_management/reminders.py tests/task_management_fakes.py tests/test_task_management_reminders.py tests/test_reminder_delivery.py tests/test_task_management_integration.py
git commit -m "refactor: move reminder policies and dispatcher to application layer"
```

---

### Task 13: Migrate AuthService, init-data/JWT adapters, and HTTP cookie adapter

**Files:**
- Create: `src/backend/application/auth/__init__.py`
- Create: `src/backend/application/auth/service.py`
- Create: `src/backend/application/auth/errors.py`
- Create: `src/backend/infrastructure/auth/__init__.py`
- Create: `src/backend/infrastructure/auth/init_data.py`
- Create: `src/backend/infrastructure/auth/tokens.py`
- Create: `src/backend/api/auth/__init__.py`
- Create: `src/backend/api/auth/cookies.py`
- Create: `src/backend/api/auth/schemas.py`
- Test: `tests/test_auth_service.py`
- Test: `tests/test_auth_cookies.py`
- Test: `tests/test_auth_api.py`
- Test: `tests/test_auth_http_security.py`
- Test: `tests/test_init_data.py`
- Test: `tests/test_auth_tokens.py`
- Deferred to Task 14: all `src/backend/auth/*.py` modules remain unchanged and become shims only after wiring switches.

**Interfaces:**
- Consumes: `AuthRepository`, `UnitOfWork`, `AuthSettings`, `InitDataValidator`, `TokenIssuer`.
- Produces: `AuthService(repository: AuthRepository, unit_of_work: UnitOfWork, validator: InitDataValidator, token_issuer: TokenIssuer, settings: AuthSettings, clock: Callable[[], datetime] | None = None)`.
- Produces: `PydanticInitDataValidator(settings: Settings)` implementing `InitDataValidator`; `JwtTokenIssuer(settings: Settings)` implementing `TokenIssuer`. Module-level `validate_init_data`, `create_access_token`, `decode_access_token`, `generate_refresh_token`, and `hash_refresh_token` remain in the infrastructure modules for compatibility.
- Produces: `set_refresh_cookie`, `clear_refresh_cookie`, `validate_auth_origin` in `backend.api.auth.cookies`; Pydantic schemas in `backend.api.auth.schemas`.
- Produces: `AuthNotConfiguredError`, `RefreshTokenGraceError` in `backend.application.auth.errors`; old `backend.auth.errors` re-exports them.

- [ ] **Step 1: Write the failing auth tests**

Add tests that refresh revocation is committed before `401`, that init-data validation returns a domain user, and that cookies still work:

```python
async def test_refresh_reuse_outside_grace_commits_revocation_before_raise() -> None:
    """Reuse detection persists the family revocation before rejecting."""
    repository = FakeAuthRepository(...)
    uow = RecordingUnitOfWork()
    service = AuthService(
        repository,
        uow,
        FakeInitDataValidator(),
        FakeTokenIssuer(),
        make_auth_settings(),
        clock=fixed_now,
    )

    with pytest.raises(UnauthorizedError):
        await service.refresh("token")

    assert repository.revoked_families == [FAMILY_ID]
    assert uow.calls == ["commit"]


def test_init_data_validator_returns_domain_user() -> None:
    """The Pydantic adapter returns a domain TelegramUser."""
    validator = PydanticInitDataValidator(make_settings())
    user = validator.validate(valid_init_data())
    assert isinstance(user, TelegramUser)
```

Keep `test_refresh_expired_session_is_revoked_and_committed`, `test_init_data.py`, `test_auth_tokens.py`, and the cookie tests; adapt constructors and imports. Define `RecordingUnitOfWork` in a shared test helper (or locally in both Task 12 and Task 13) with a `calls` list; `FakeInitDataValidator`/`FakeTokenIssuer` are small local doubles implementing the ports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_auth_service.py tests/test_auth_cookies.py tests/test_init_data.py tests/test_auth_tokens.py -q`
Expected: FAIL because `AuthService` still imports ORM/Settings, init data returns a Pydantic model, and commit helpers live in the repository.

- [ ] **Step 3: Implement the service and adapters**

Move `AuthService` to `application/auth/service.py`; replace ORM entities with domain auth entities, replace `Settings` with `AuthSettings`, and replace `revoke_*_and_commit` with `revoke_*` + `unit_of_work.commit()`. Introduce `TelegramIdentity` for login (`user` + `profile`) and `TelegramUser` as a plain domain dataclass. Copy the Pydantic init-data parsing into `PydanticInitDataValidator` and keep module-level `validate_init_data` in the infrastructure module for compatibility; keep the same HMAC, freshness, bot rejection, and error messages. Copy JWT signing/verification into `JwtTokenIssuer` and keep module-level `create_access_token`/`decode_access_token` with their existing `Settings` signatures; keep the same claims, leeway, issuer, and TTLs. Pure refresh-token generation/hash moves to `domain/auth/tokens.py` with compatibility re-exports. Move FastAPI cookie/origin functions to `api/auth/cookies.py` and Pydantic schemas to `api/auth/schemas.py`. Do not modify the old auth modules yet; they keep serving API wiring until Task 14. Update auth tests and fakes to the new modules.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_auth_service.py tests/test_auth_cookies.py tests/test_auth_api.py tests/test_auth_http_security.py tests/test_init_data.py tests/test_auth_tokens.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/application/auth src/backend/infrastructure/auth src/backend/api/auth src/backend/domain/auth tests/test_auth_service.py tests/test_auth_cookies.py tests/test_auth_api.py tests/test_auth_http_security.py tests/test_init_data.py tests/test_auth_tokens.py
git commit -m "refactor: move auth service and adapters to clean layers"
```

---

### Task 14: Single composition root

**Files:**
- Create: `src/backend/composition/__init__.py`
- Create: `src/backend/composition/task_management.py`
- Create: `src/backend/composition/auth.py`
- Create: `src/backend/infrastructure/telegram/__init__.py`
- Create: `src/backend/infrastructure/telegram/sender.py`
- Modify: `src/backend/api/deps.py`
- Modify: `src/backend/api/task_schemas.py` (direct ORM annotations -> domain types)
- Modify: `src/backend/api/routes/labels.py` (direct ORM annotations -> domain types)
- Modify: `src/backend/tasks/generation.py`
- Modify: `src/backend/tasks/delivery.py`
- Modify: `src/backend/tasks/telegram.py` (re-export infrastructure sender)
- Modify: `src/backend/infrastructure/persistence/sqlalchemy/legacy.py` (composite facade)
- Modify all old compatibility modules:
  - `src/backend/task_management/{enums,date_bounds,schedule,palette,labels,projects,series,tasks,reminders,ports,repository}.py`
  - `src/backend/auth/{service,repository,cookies,tokens,init_data,errors,schemas}.py`
- Test: `tests/test_composition.py`
- Test: `tests/test_deps.py`
- Test: `tests/test_task_generation.py`
- Test: `tests/test_reminder_delivery.py`
- Test: `tests/test_task_management_integration.py`
- Test: `tests/test_task_management_repository.py`

**Interfaces:**
- Produces: `TaskManagementServices`, `build_task_management_services`, `build_series_service`, `build_palette_service`, `build_reminder_dispatcher` in `backend.composition.task_management`.
- Produces: `build_auth_service(session, settings, *, clock=None) -> AuthService` in `backend.composition.auth`; it maps `Settings -> AuthSettings` and constructs `PydanticInitDataValidator`, `JwtTokenIssuer`, `SqlAlchemyAuthRepository`, and `SqlAlchemyUnitOfWork`.
- `build_palette_service(session: AsyncSession) -> PaletteService` is used by admin and returns a service bound to the supplied session.
- Produces: `TelegramBotSender` in `backend.infrastructure.telegram.sender`; old `backend.tasks.telegram` re-exports it and keeps `TelegramSender` as a compatibility alias.
- Consumes: aggregate adapters, `SqlAlchemyUnitOfWork`, services.

- [ ] **Step 1: Write the failing composition tests**

```python
async def test_task_composition_uses_one_session_and_shared_uow() -> None:
    """API and worker builders choose the same aggregate adapters."""
    session = RecordingSession()
    services = build_task_management_services(session, clock=fixed_now)
    assert isinstance(services.tasks, TaskService)
    assert services.tasks._repository is not services.projects._repository
    assert services.tasks._unit_of_work is services.series._unit_of_work


async def test_generation_and_delivery_use_composition() -> None:
    """Taskiq entrypoints build services through the composition root."""
    source = Path("src/backend/tasks/generation.py").read_text()
    assert "build_series_service" in source
    assert "SqlAlchemyTaskManagementRepository" not in source


def test_auth_composition_builds_auth_service() -> None:
    """Auth composition maps settings and selects the auth adapters."""
    service = build_auth_service(RecordingSession(), make_settings())
    assert isinstance(service, AuthService)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_composition.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.composition'`.

- [ ] **Step 3: Implement the composition root**

Build aggregate adapters from one `AsyncSession`, one `SqlAlchemyUnitOfWork`, and one clock. Wire services with narrow ports; `SeriesService` receives the account repository in addition to series/task/reminder repositories, project/label readers, and the shared unit of work. `build_auth_service` maps `Settings -> AuthSettings`, constructs `PydanticInitDataValidator`, `JwtTokenIssuer`, `SqlAlchemyAuthRepository`, and the shared `SqlAlchemyUnitOfWork`. Update `api/deps.py`, `tasks/generation.py`, and `tasks/delivery.py` to use the factories. Keep dependency names (`get_task_service`, etc.) and `app.state` access unchanged so tests keep overriding them. Move `TelegramBotSender` and its HTTP client code into `infrastructure/telegram/sender.py`; make `tasks/telegram.py` a re-export shim and keep `TelegramSender` as an alias of the application `ReminderSender` port. Update direct ORM annotations in `api/deps.py`, `api/task_schemas.py`, and `api/routes/labels.py` to domain types so mypy stays green before the full mapper extraction in Tasks 15-16. Add `legacy.py` with the composite facade delegating to aggregate adapters. Only after the wiring is switched, replace every old `task_management/*` and `auth/*` module with a thin re-export shim: old `task_management/ports.py` re-exports the composite protocol from `application/ports/legacy.py`, old `task_management/repository.py` re-exports the composite adapter, and old service/auth modules re-export the new application/domain/infrastructure symbols.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_composition.py tests/test_deps.py tests/test_task_generation.py tests/test_reminder_delivery.py tests/test_task_management_integration.py tests/test_task_management_repository.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/composition src/backend/infrastructure/telegram src/backend/api/deps.py src/backend/api/task_schemas.py src/backend/api/routes/labels.py src/backend/tasks/generation.py src/backend/tasks/delivery.py src/backend/tasks/telegram.py src/backend/infrastructure/persistence/sqlalchemy/legacy.py src/backend/task_management src/backend/auth tests/test_composition.py tests/test_deps.py tests/test_task_generation.py tests/test_reminder_delivery.py tests/test_task_management_integration.py tests/test_task_management_repository.py
git commit -m "refactor: consolidate composition root for API and workers"
```

---

### Task 15: API mappers and thin task/project/series routes

**Files:**
- Create: `src/backend/api/mappers/__init__.py`
- Create: `src/backend/api/mappers/task_management.py`
- Create: `src/backend/api/queries/__init__.py`
- Create: `src/backend/api/queries/tasks.py`
- Create: `src/backend/api/queries/projects.py`
- Modify: `src/backend/api/task_schemas.py` (remove ORM imports)
- Modify: `src/backend/api/routes/tasks.py`
- Modify: `src/backend/api/routes/projects.py`
- Modify: `src/backend/api/routes/task_series.py`
- Modify: `src/backend/api/routes/color_palette.py`
- Test: `tests/test_api_mappers.py`
- Test: `tests/test_tasks_api.py`
- Test: `tests/test_projects_api.py`
- Test: `tests/test_task_series_api.py`
- Test: `tests/test_color_palette_api.py`

**Interfaces:**
- Produces: mapper functions from spec section 7.6.
- Produces: `parse_task_cursor`, `encode_task_cursor`, `parse_project_cursor`, `encode_project_cursor` in `api/queries`.
- Consumes: domain DTOs and `TaskResponse`, `ProjectResponse`, `SeriesResponse`, `ColorResponse`.

- [ ] **Step 1: Write the failing mapper tests**

Assert exact response shapes from domain DTOs:

```python
def test_task_response_maps_domain_detail() -> None:
    """Task response mapping does not depend on ORM rows."""
    detail = make_domain_task_detail()
    response = task_response(detail)
    assert response.id == detail.task.id
    assert response.recurrence is detail.recurrence
    assert response.reminders[0].kind == "before_due"


def test_task_mapper_module_does_not_import_orm() -> None:
    """API schemas and mappers never import backend.models."""
    source = Path("src/backend/api/task_schemas.py").read_text()
    assert "backend.models" not in source
```

Add analogous tests for project/series/color.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_api_mappers.py -q`
Expected: FAIL with import errors for `backend.api.mappers`.

- [ ] **Step 3: Implement mappers, queries, and thin routes**

Move cursor parsing/fingerprinting from `routes/tasks.py` and `routes/projects.py` into `api/queries`. Replace `_task_response`, `_project_response`, `_series_response`, and `ReminderResponse.from_row` with mappers. Remove `backend.models` imports from `task_schemas.py` and routes. Keep OpenAPI parameters, response models, status codes, and docstrings unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_api_mappers.py tests/test_tasks_api.py tests/test_projects_api.py tests/test_task_series_api.py tests/test_color_palette_api.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/api/mappers src/backend/api/queries src/backend/api/task_schemas.py src/backend/api/routes/tasks.py src/backend/api/routes/projects.py src/backend/api/routes/task_series.py src/backend/api/routes/color_palette.py tests/test_api_mappers.py tests/test_tasks_api.py tests/test_projects_api.py tests/test_task_series_api.py tests/test_color_palette_api.py
git commit -m "refactor: add API mappers and thin task routes"
```

---

### Task 16: Thin label/auth routes and domain current user

**Files:**
- Create: `src/backend/api/mappers/auth.py`
- Modify: `src/backend/api/routes/labels.py`
- Modify: `src/backend/api/routes/auth.py`
- Modify: `src/backend/api/deps.py`
- Test: `tests/test_labels_api.py`
- Test: `tests/test_auth_api.py`
- Test: `tests/test_route_auth_matrix.py`

**Interfaces:**
- Produces: `user_response(User) -> UserResponse`, `profile_response(TelegramProfile) -> TelegramProfileResponse`, `auth_response(LoginResult, expires_in) -> AuthResponse`, `token_response(TokenPair) -> TokenResponse`.
- Produces: `CurrentUserDep = Annotated[User, Depends(get_current_user)]`.
- Consumes: domain auth entities, `AuthService`.

- [ ] **Step 1: Write the failing route tests**

Add tests that auth responses still serialize the same fields and that `get_current_user` returns a domain DTO:

```python
async def test_current_user_dependency_returns_domain_user() -> None:
    """The authenticated principal is not an ORM row."""
    user = await get_current_user(claims=_claims(), service=FakeAuthService())
    assert isinstance(user, User)


def test_label_route_does_not_import_orm() -> None:
    """Label routes use mappers instead of ORM validation."""
    source = Path("src/backend/api/routes/labels.py").read_text()
    assert "backend.models" not in source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_auth_api.py tests/test_labels_api.py -q`
Expected: FAIL because routes still import ORM models and `get_current_user` returns `User`.

- [ ] **Step 3: Implement auth mappers and thin routes**

Replace `UserResponse(...)`, `TelegramProfileResponse.model_validate(...)`, and label color loops with mappers. `get_current_user` loads a domain `User` from the service. Keep cookie handling, origin guard, error responses, and OpenAPI docs unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_labels_api.py tests/test_auth_api.py tests/test_route_auth_matrix.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/api/mappers/auth.py src/backend/api/routes/labels.py src/backend/api/routes/auth.py src/backend/api/deps.py tests/test_labels_api.py tests/test_auth_api.py tests/test_route_auth_matrix.py
git commit -m "refactor: map auth responses and use domain current user"
```

---

### Task 17: Reuse palette policy in SQLAdmin

**Files:**
- Modify: `src/backend/admin/views.py`
- Test: `tests/test_admin_views.py`
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `build_palette_service(session)` and `PaletteService.ensure_can_deactivate/ensure_can_delete`.
- Produces: unchanged admin behavior with no direct count SQL.

- [ ] **Step 1: Write the failing admin test**

```python
async def test_palette_admin_deactivation_uses_palette_policy(monkeypatch) -> None:
    """The admin delegates last-active-color checks to the palette service."""
    called: list[int] = []

    async def fake_ensure(self, color_id: int) -> None:
        called.append(color_id)

    monkeypatch.setattr(PaletteService, "ensure_can_deactivate", fake_ensure)
    admin = ColorPaletteAdmin(...)
    await admin.on_model_change({"is_active": False}, make_color_row(id=3), False, request)
    assert called == [3]
```

Keep the existing slug, last-active, and FK-conflict tests.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_admin_views.py -q -k 'palette_admin'`
Expected: FAIL because the admin still executes its own count query.

- [ ] **Step 3: Implement the admin policy reuse**

In `on_model_change`, keep the immutable-slug check; when `is_active` changes to `False`, build a palette service on `self.session_maker()` and call `ensure_can_deactivate(model.id)`. In `on_model_delete`, call `ensure_can_delete(model.id)` for active colors. Remove the direct `select(func.count())` blocks. Keep `IntegrityError` translation in `delete_model`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_admin_views.py tests/test_admin.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/admin/views.py tests/test_admin_views.py tests/test_admin.py
git commit -m "refactor: reuse palette policy in admin"
```

---

### Task 18: Final architecture enforcement, OpenAPI contract, and cleanup

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_openapi_contract.py` (baseline from Task 0)
- Modify: `README.md` (layered architecture note)
- Modify: old compatibility modules (`src/backend/task_management/*.py`, `src/backend/auth/*.py`, `src/backend/tasks/telegram.py`) to ensure they are thin re-exports only
- Test: full suite

**Interfaces:**
- Produces: architecture tests for all inner-layer import rules and ORM-free API.
- Produces: an OpenAPI contract test that pins paths, methods, operation IDs, query parameters, response schema names, and documented error statuses.

- [ ] **Step 1: Write the failing final guard tests**

Extend `tests/test_architecture.py`:

```python
def test_api_does_not_import_orm_models() -> None:
    """API schemas, mappers, and routes do not depend on persistence models."""
    for path in Path("src/backend/api").rglob("*.py"):
        source = path.read_text()
        assert "backend.models" not in source


def test_compatibility_modules_are_shims_only() -> None:
    """Old module paths re-export new layers instead of owning implementations."""
    for path in [
        Path("src/backend/task_management/enums.py"),
        Path("src/backend/task_management/date_bounds.py"),
        Path("src/backend/task_management/schedule.py"),
        Path("src/backend/task_management/tasks.py"),
        Path("src/backend/task_management/palette.py"),
        Path("src/backend/task_management/labels.py"),
        Path("src/backend/task_management/projects.py"),
        Path("src/backend/task_management/series.py"),
        Path("src/backend/task_management/reminders.py"),
        Path("src/backend/task_management/ports.py"),
        Path("src/backend/task_management/repository.py"),
        Path("src/backend/auth/service.py"),
        Path("src/backend/auth/repository.py"),
        Path("src/backend/auth/cookies.py"),
        Path("src/backend/auth/tokens.py"),
        Path("src/backend/auth/init_data.py"),
        Path("src/backend/auth/errors.py"),
        Path("src/backend/auth/schemas.py"),
        Path("src/backend/tasks/telegram.py"),
    ]:
        source = path.read_text()
        assert "class " not in source
```

Keep the Task 0 baseline test and extend it if new contract-sensitive fields are found:

```python
def test_openapi_surface_is_unchanged(app: FastAPI) -> None:
    """Paths, methods, operation IDs, and documented errors stay stable."""
    spec = app.openapi()
    assert set(spec["paths"]) == EXPECTED_PATHS
    assert spec["paths"]["/api/v1/tasks"]["get"]["operationId"] == "tasks-list_tasks"
    assert "400" in spec["paths"]["/api/v1/tasks"]["get"]["responses"]
```

Use the checked-in expected paths/operation IDs captured in Task 0, before any refactor.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_architecture.py tests/test_openapi_contract.py -q`
Expected: FAIL until the remaining ORM imports and non-shim modules are removed.

- [ ] **Step 3: Finish cleanup and documentation**

Remove remaining ORM imports from `api/`; ensure every old module is a pure re-export shim with `__all__`; remove transitional `Any`/`cast` from tests where possible; add a short layered-architecture section to `README.md` pointing to the spec. Do not change public API text.

- [ ] **Step 4: Run the full verification suite**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
poetry run pytest --cov=backend --cov-report=term-missing
graphify update .
```

Expected: all commands exit with status 0; coverage does not drop on the refactored modules.

- [ ] **Step 5: Inspect the working tree and commit only intended files**

```bash
git status --short
git add tests/test_architecture.py tests/test_openapi_contract.py README.md src/backend
git commit -m "refactor: enforce clean architecture boundaries and contract tests"
```

Do not stage unrelated pre-existing changes.

---

## Final Verification

- [ ] `poetry run pytest -q`
- [ ] `poetry run ruff check .`
- [ ] `poetry run ruff format --check src tests`
- [ ] `poetry run mypy src tests`
- [ ] `poetry run pytest --cov=backend --cov-report=term-missing`
- [ ] `graphify update .`
- [ ] `git status --short` shows no unintended staged files.
- [ ] Architecture tests prove `domain`/`application` purity and ORM-free API.
- [ ] OpenAPI contract test and `tests/test_route_auth_matrix.py` pass unchanged.
- [ ] The spec at `docs/superpowers/specs/2026-11-10-clean-architecture-refactor-design.md` matches the implemented behavior.
