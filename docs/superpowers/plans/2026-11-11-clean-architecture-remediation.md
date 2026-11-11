# Clean Architecture Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the dead legacy composite facade, split the task and series god-services by responsibility, move presentation/transport policy to its proper layers, and make the composition root single and complete without changing HTTP contracts, the database schema, business rules, or transaction boundaries.

**Architecture:** Incremental strangler-style cleanup. Each task adds or moves one responsibility, switches its consumers, runs the full suite, and leaves the old path either deleted or as a thin compatibility re-export. Shared task validation/detail assembly becomes application collaborators; generation moves from `SeriesService` into `SeriesMaterializer` + `SeriesGenerationService`; `TokenVerifier` and `ProjectReminderCommands` close the remaining composition/DIP gaps.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL, Taskiq, pytest, mypy, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-clean-architecture-remediation-design.md`

## Global Constraints

- No new runtime or dev dependencies.
- No database migration: no table, column, index, constraint, or seed changes.
- HTTP contracts are frozen: paths, methods, status codes, JSON bodies, error envelope, `request_id`, cursor format, OpenAPI operation IDs, and documented error statuses.
- Business rules are frozen: recurrence, reminder slots, pagination, soft/hard delete, archive/unarchive, tenancy, limits, and Russian error descriptions.
- Transaction boundaries are frozen: request commit in `Database.session()`, per-series commit/rollback in generation, dispatcher commit points, auth commit-on-error, series-before-task lock order, and flush-before-dedupe in `create_from_task`.
- `domain` must not import FastAPI, Starlette, SQLAlchemy, Taskiq, Pydantic, `backend.core.errors`, `backend.models`, `backend.db`, `backend.infrastructure`, `backend.api`, `backend.tasks`, `backend.admin`, or `backend.composition`.
- `application` must not import FastAPI, Starlette, SQLAlchemy, Taskiq, Pydantic, ORM, concrete adapters, or the composition root.
- `api` must not import ORM models or concrete infrastructure adapters; concrete adapters are chosen only in `backend/composition`.
- Remaining compatibility shims stay thin re-exports. Only `application/ports/legacy.py`, `infrastructure/persistence/sqlalchemy/legacy.py`, `backend/task_management/ports.py`, and `backend/task_management/repository.py` are removed.
- Every new or changed function/class gets an English docstring in the project template; stale docstrings are updated in the same change.
- Do not stage unrelated pre-existing working-tree changes.
- Verify after every task: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`.
- After the final task: `graphify update .` and `git status --short` must show only intended changes.

## Review Focus

- Reminder text must stay byte-identical: Russian copy, emoji, HTML escaping, relative dates, countdown, priority lines, and project suffix for before-due, scheduled, daily, and periodic reminders.
- Series generation must keep per-series commit/rollback, candidate-id snapshots, `FOR UPDATE SKIP LOCKED`, `(series_id, due_at)` idempotency, and flush-before-dedupe in `create_from_task`.
- Project unarchive must recompute the same task reminder slots, keep series templates at `next_run_at=None`, retarget scheduled series timezones, and keep the same batch-query/flush count.
- Auth verification must return the same `AccessTokenClaims` and the same `401` behavior for missing, malformed, expired, wrong-issuer, and wrong-audience tokens.
- Composition must build one task-management graph per request; all services in that graph must share the same session and unit of work without changing commit/flush timing.

---

### Task 1: Remove the dead legacy composite facade

**Files:**
- Delete: `src/backend/application/ports/legacy.py`
- Delete: `src/backend/infrastructure/persistence/sqlalchemy/legacy.py`
- Delete: `src/backend/task_management/ports.py`
- Delete: `src/backend/task_management/repository.py`
- Modify: `tests/test_architecture.py:29-49`
- Modify: `tests/test_port_contracts.py:7-10,31-260,301-308`
- Modify: `tests/test_composition.py:21,41-43,267-315`

**Interfaces:**
- Consumes: aggregate protocols in `backend.application.ports.task_management` and their SQLAlchemy adapters.
- Produces: no composite production protocol or adapter; the aggregate contracts are the only persistence contracts.

- [ ] **Step 1: Write the failing absence test**

Add to `tests/test_architecture.py`:

```python
REMOVED_LEGACY_MODULES = (
    Path("src/backend/application/ports/legacy.py"),
    Path("src/backend/infrastructure/persistence/sqlalchemy/legacy.py"),
    Path("src/backend/task_management/ports.py"),
    Path("src/backend/task_management/repository.py"),
)


def test_removed_legacy_composite_modules_are_absent() -> None:
    """The dead composite facade is not kept as a second persistence contract."""
    for path in REMOVED_LEGACY_MODULES:
        assert not path.exists(), f"{path} must be removed"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `poetry run pytest tests/test_architecture.py::test_removed_legacy_composite_modules_are_absent -q`
Expected: FAIL because all four files still exist.

- [ ] **Step 3: Delete the modules and update tests**

Remove the four files. In `tests/test_port_contracts.py`:

- remove `from backend.application.ports import legacy`;
- rename `LEGACY_FAT_PORT_METHODS` to `ALL_AGGREGATE_METHODS`;
- delete `test_legacy_facade_composes_all_aggregates_and_unit_of_work`;
- keep `test_aggregate_protocols_partition_the_legacy_contract_without_duplicates`, comparing the aggregate union with `ALL_AGGREGATE_METHODS | SAVE_METHODS`.

In `tests/test_composition.py`, remove imports and assertions for
`TaskManagementRepository` and `SqlAlchemyTaskManagementRepository`, and
delete `test_legacy_composite_adapter_shares_one_session_across_aggregates`.
Keep `tests/task_management_fakes.py` unchanged; it is now a structural test
double for the aggregate protocols.

In `tests/test_architecture.py`, remove the two deleted shim paths from
`COMPATIBILITY_SHIMS`.

- [ ] **Step 4: Run the target tests**

Run: `poetry run pytest tests/test_architecture.py tests/test_port_contracts.py tests/test_composition.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite and commit**

Run: `poetry run pytest -q`
Expected: PASS.

```bash
git add src/backend/application/ports/legacy.py \
  src/backend/infrastructure/persistence/sqlalchemy/legacy.py \
  src/backend/task_management/ports.py \
  src/backend/task_management/repository.py \
  tests/test_architecture.py tests/test_port_contracts.py tests/test_composition.py
git commit -m "refactor: remove dead legacy composite repository"
```

---

### Task 2: Move reminder text to an application presenter

**Files:**
- Create: `src/backend/application/task_management/reminder_presenter.py`
- Modify: `src/backend/domain/task_management/policies.py:1-40,517-716`
- Modify: `src/backend/application/task_management/reminders.py:20-30,220-230`
- Modify: `src/backend/task_management/reminders.py:1-40`
- Test: `tests/test_task_management_reminders.py:1-20,200-370` (move the
  direct `build_reminder_text` import to the presenter module)

**Interfaces:**
- Consumes: `ReminderContent`, `ReminderKind`, and the current text constants/helpers.
- Produces: `build_reminder_text(content: ReminderContent, *, kind: ReminderKind, offset_minutes: int | None, now: datetime, timezone_name: str | None) -> str` in `backend.application.task_management.reminder_presenter`.

- [ ] **Step 1: Write the failing placement test**

Add to `tests/test_task_management_reminders.py`:

```python
def test_reminder_text_is_presented_by_application_not_domain() -> None:
    """User-facing message rendering is an application presenter concern."""
    import backend.domain.task_management.policies as policies
    from backend.application.task_management.reminder_presenter import (
        build_reminder_text as presenter_build_reminder_text,
    )

    assert not hasattr(policies, "build_reminder_text")
    assert callable(presenter_build_reminder_text)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `poetry run pytest tests/test_task_management_reminders.py::test_reminder_text_is_presented_by_application_not_domain -q`
Expected: FAIL with `ModuleNotFoundError` for the presenter.

- [ ] **Step 3: Move the renderer**

Move `build_reminder_text`, `_format_description`, `_countdown_minutes`,
`_format_duration`, `_plural`, `_format_due`, `_MESSAGE_HEADER`,
`_DESCRIPTION_LIMIT`, `_MONTHS`, and `_PRIORITY_LINES` verbatim to
`reminder_presenter.py`. Remove the `html.escape` import and the removed
constants from `domain/task_management/policies.py`.

Update `application/task_management/reminders.py` to import
`build_reminder_text` from `backend.application.task_management.reminder_presenter`.
Update `backend/task_management/reminders.py` to re-export
`build_reminder_text` from the presenter, not from domain policies. Update the
direct import in `tests/test_task_management_reminders.py` to the presenter
module; do not change any expected message text.

- [ ] **Step 4: Run reminder tests and the full suite**

Run: `poetry run pytest tests/test_task_management_reminders.py tests/test_task_management_integration.py -q`
Expected: PASS with byte-identical message assertions.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/application/task_management/reminder_presenter.py \
  src/backend/application/task_management/reminders.py \
  src/backend/domain/task_management/policies.py \
  src/backend/task_management/reminders.py \
  tests/test_task_management_reminders.py
git commit -m "refactor: move reminder rendering to application presenter"
```

---

### Task 3: Introduce domain reminder errors

**Files:**
- Create: `src/backend/domain/task_management/errors.py`
- Modify: `src/backend/domain/task_management/policies.py:1-30,718-1007`
- Modify: `src/backend/application/task_management/tasks.py` (catch/map at `create`, `update`, `uncomplete`, `_rebuild_task_reminders`)
- Modify: `src/backend/application/task_management/series.py` (catch/map at `create`, `create_from_task`, `update`, `_instance_reminder`)
- Modify: `src/backend/infrastructure/persistence/sqlalchemy/reminder_repository.py` (resume catch)
- Test: `tests/test_task_management_reminders.py`
- Test: `tests/test_architecture.py`

**Interfaces:**
- Consumes: current reminder validation and scheduling rules.
- Produces: `ReminderConfigurationError(ValueError)` in `backend.domain.task_management.errors`; application services still raise `BadRequestError` with the same technical messages.

- [ ] **Step 1: Write the failing domain-error tests**

Add to `tests/test_task_management_reminders.py`:

```python
def test_domain_reminder_validation_uses_domain_error() -> None:
    """Domain policies do not raise the transport-level BadRequestError."""
    from backend.domain.task_management.errors import ReminderConfigurationError
    from backend.domain.task_management.models import ReminderInput
    from backend.domain.task_management.policies import validate_reminder_inputs

    with pytest.raises(ReminderConfigurationError):
        validate_reminder_inputs(
            [ReminderInput(kind=ReminderKind.BEFORE_DUE, enabled=True)]
        )
```

Add to `tests/test_architecture.py`:

```python
def test_domain_does_not_import_core_errors() -> None:
    """Domain policies are independent of the transport error registry."""
    for path in Path("src/backend/domain").rglob("*.py"):
        for imported in _imports(path):
            assert imported != "backend.core.errors", f"{path}: {imported}"
            assert not imported.startswith("backend.core.errors."), f"{path}: {imported}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_reminders.py::test_domain_reminder_validation_uses_domain_error tests/test_architecture.py::test_domain_does_not_import_core_errors -q`
Expected: FAIL with missing `ReminderConfigurationError` and the `core.errors` import.

- [ ] **Step 3: Add the domain error and map it in application**

Create `domain/task_management/errors.py`:

```python
class ReminderConfigurationError(ValueError):
    """Invalid reminder rule or schedule outside the supported range."""
```

In `policies.py`, replace every `BadRequestError(...)` with
`ReminderConfigurationError(...)` and remove the `backend.core.errors` import.
Preserve the exact message strings.

At application boundaries, catch `ReminderConfigurationError` and re-raise
`BadRequestError(str(error)) from error`:

- `TaskService.create` and `TaskService.update` (the whole update body may be
  wrapped, but only this exception is translated);
- `TaskService.uncomplete` around `rebuild_reminder_slot`;
- `SeriesService.create`, `SeriesService.create_from_task`, and
  `SeriesService.update` around reminder validation/rebuild calls;
- `SeriesService._instance_reminder` or its caller around
  `build_task_reminder`;
- `ReminderDispatcher` may continue using its generic per-candidate exception
  handling; no `BadRequestError` translation is needed there.

In `reminder_repository.py`, replace `except (ZoneInfoNotFoundError, ValueError, BadRequestError)`
with `except (ZoneInfoNotFoundError, ValueError)` (the new domain error is a
`ValueError`) and remove the now-unused `BadRequestError` import.

- [ ] **Step 4: Run the target tests and the full suite**

Run: `poetry run pytest tests/test_task_management_reminders.py tests/test_task_management_schedule.py tests/test_task_management_tasks.py tests/test_task_management_series.py tests/test_architecture.py -q`
Expected: PASS. Application tests still assert `BadRequestError`; domain tests assert `ReminderConfigurationError`.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/domain/task_management/errors.py \
  src/backend/domain/task_management/policies.py \
  src/backend/application/task_management/tasks.py \
  src/backend/application/task_management/series.py \
  src/backend/infrastructure/persistence/sqlalchemy/reminder_repository.py \
  tests/test_task_management_reminders.py tests/test_architecture.py
git commit -m "refactor: use domain errors for reminder validation"
```

---

### Task 4: Move HTTP status codes out of core errors

**Files:**
- Create: `src/backend/api/errors/status.py`
- Modify: `src/backend/core/errors.py:30-150`
- Modify: `src/backend/api/errors/mapping.py:60-240`
- Modify: `src/backend/api/errors/openapi.py:50-70`
- Test: `tests/test_error_codes.py`
- Test: `tests/test_error_mapping.py`

**Interfaces:**
- Consumes: `ErrorCode`, `ERROR_SPECS`, `get_error_spec`.
- Produces: `ERROR_STATUS_CODES: dict[ErrorCode, int]` and
  `status_code_for(code: ErrorCode) -> int` in `backend.api.errors.status`.

- [ ] **Step 1: Write the failing status-map tests**

Add to `tests/test_error_codes.py`:

```python
from backend.api.errors.status import ERROR_STATUS_CODES, status_code_for


def test_error_status_map_covers_every_code() -> None:
    """Transport statuses are registered for every machine-readable code."""
    assert set(ERROR_STATUS_CODES) == set(ErrorCode)
    assert all(400 <= status <= 599 for status in ERROR_STATUS_CODES.values())


def test_error_spec_no_longer_owns_http_status() -> None:
    """The core error registry is transport-independent."""
    spec = get_error_spec(ErrorCode.BAD_REQUEST)
    assert not hasattr(spec, "status_code")
    assert status_code_for(ErrorCode.BAD_REQUEST) == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_error_codes.py::test_error_status_map_covers_every_code tests/test_error_codes.py::test_error_spec_no_longer_owns_http_status -q`
Expected: FAIL with missing `backend.api.errors.status`.

- [ ] **Step 3: Move the status mapping**

Create `api/errors/status.py` with the complete `ERROR_STATUS_CODES` map
copied from the current `ERROR_SPECS` values. Remove `status_code` from
`ErrorSpec` and from every `ERROR_SPECS` entry in `core/errors.py`; update the
`ErrorSpec` docstring so it no longer claims to describe transport behavior.

Update `api/errors/mapping.py` to use `status_code_for(code)` instead of
`spec.status_code` in `payload_from_app_error`, `payload_from_http_exception`,
`payload_from_sqlalchemy_error`, and the other payload builders. Update
`api/errors/openapi.py` to group documented errors by
`status_code_for(ErrorCode(...))`.

Update the status assertions in `tests/test_error_codes.py` and
`tests/test_error_mapping.py` to use `ERROR_STATUS_CODES`/`status_code_for`.
The wire responses must not change.

- [ ] **Step 4: Run error tests and the full suite**

Run: `poetry run pytest tests/test_error_codes.py tests/test_error_mapping.py tests/test_errors.py tests/test_error_middleware.py tests/test_openapi_errors.py -q`
Expected: PASS.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/api/errors/status.py \
  src/backend/api/errors/mapping.py src/backend/api/errors/openapi.py \
  src/backend/core/errors.py tests/test_error_codes.py tests/test_error_mapping.py
git commit -m "refactor: move HTTP error statuses to API layer"
```

---

### Task 5: Add the TokenVerifier port and composition factory

**Files:**
- Modify: `src/backend/application/ports/auth.py:295-372`
- Modify: `src/backend/infrastructure/auth/tokens.py:181-309`
- Modify: `src/backend/composition/auth.py`
- Modify: `src/backend/main.py:14-30,132-180`
- Modify: `src/backend/api/deps.py:26,223-258`
- Test: `tests/test_port_contracts.py`
- Test: `tests/test_auth_tokens.py`
- Test: `tests/test_composition.py`
- Test: `tests/test_architecture.py`

**Interfaces:**
- Consumes: `decode_access_token` and current auth settings.
- Produces: `TokenVerifier.verify_access_token(raw_token: str) -> AccessTokenClaims`; `JwtTokenVerifier(settings: Settings)`; `build_token_verifier(settings: Settings) -> TokenVerifier`; `app.state.token_verifier`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_port_contracts.py`:

```python
def test_token_verifier_is_a_narrow_port() -> None:
    """Access-token verification is separate from issuance."""
    assert _methods(auth_ports.TokenVerifier) == {"verify_access_token"}
```

Add to `tests/test_auth_tokens.py`:

```python
def test_jwt_token_verifier_matches_decode_access_token() -> None:
    """The verifier returns the same claims as the compatibility decoder."""
    settings = make_settings()
    verifier = JwtTokenVerifier(settings)
    token, _ = create_access_token(
        7, 11, 7007, now=datetime(2026, 11, 4, 12, 0, tzinfo=UTC), settings=settings
    )
    assert verifier.verify_access_token(token) == decode_access_token(token, settings=settings)
```

Add to `tests/test_architecture.py`:

```python
def test_api_does_not_import_concrete_infrastructure() -> None:
    """API dependencies choose concrete adapters only through composition."""
    for path in Path("src/backend/api").rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith("backend.infrastructure"), f"{path}: {imported}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_port_contracts.py::test_token_verifier_is_a_narrow_port tests/test_auth_tokens.py::test_jwt_token_verifier_matches_decode_access_token tests/test_architecture.py::test_api_does_not_import_concrete_infrastructure -q`
Expected: FAIL with missing `TokenVerifier` and the `api/deps.py` infrastructure import.

- [ ] **Step 3: Implement the port, adapter, and composition factory**

Add `TokenVerifier` to `application/ports/auth.py` with a docstring contract.
Add `JwtTokenVerifier` to `infrastructure/auth/tokens.py`:

```python
class JwtTokenVerifier:
    """Verify access tokens through the existing JWT adapter."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def verify_access_token(self, raw_token: str) -> AccessTokenClaims:
        return decode_access_token(raw_token, settings=self._settings)
```

Add `build_token_verifier(settings: Settings) -> TokenVerifier` to
`composition/auth.py`. In `main.create_app`, set
`app.state.token_verifier = build_token_verifier(settings)` next to
`app.state.settings`.

In `api/deps.get_current_access`, replace the direct import with:

```python
verifier = request.app.state.token_verifier
return verifier.verify_access_token(credentials.credentials)
```

Update `tests/test_composition.py` app double if it constructs a request for
`get_current_access` (add a `token_verifier` attribute).

- [ ] **Step 4: Run auth/composition/architecture tests and the full suite**

Run: `poetry run pytest tests/test_auth_tokens.py tests/test_auth_api.py tests/test_auth_service.py tests/test_composition.py tests/test_deps.py tests/test_port_contracts.py tests/test_architecture.py -q`
Expected: PASS.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/application/ports/auth.py \
  src/backend/infrastructure/auth/tokens.py src/backend/composition/auth.py \
  src/backend/main.py src/backend/api/deps.py \
  tests/test_port_contracts.py tests/test_auth_tokens.py \
  tests/test_composition.py tests/test_architecture.py
git commit -m "refactor: add token verifier port and composition factory"
```

---

### Task 6: Extract shared TaskReferences, TaskDetails, and task input helpers

**Files:**
- Create: `src/backend/application/task_management/task_references.py`
- Create: `src/backend/application/task_management/task_details.py`
- Create: `src/backend/application/task_management/task_inputs.py`
- Modify: `src/backend/application/task_management/tasks.py:58-105,1144-1511`
- Modify: `src/backend/application/task_management/series.py:56-96,1097-1264`
- Modify: `src/backend/composition/task_management.py:101-136`
- Test: `tests/test_task_references.py` (create)
- Test: `tests/test_task_details.py` (create)
- Test: `tests/test_task_management_tasks.py:45-80`
- Test: `tests/test_task_management_series.py:23-43`
- Test: `tests/test_task_management_integration.py:35-95`

**Interfaces:**
- Consumes: `ProjectReader`, `LabelReader`, `AccountRepository`, `TaskRepository`, `SeriesRepository`.
- Produces:

```python
class TaskReferences:
    async def validate_project(self, user_id: int, project_id: int | None) -> None: ...
    async def validate_labels(
        self, user_id: int, label_ids: Sequence[int]
    ) -> tuple[int, ...]: ...
    async def user_timezone(self, user_id: int) -> ZoneInfo: ...
    def named_timezone(self, name: str) -> ZoneInfo: ...

class TaskDetails:
    async def detail(self, user_id: int, task: Task) -> TaskDetail: ...
    async def details(self, user_id: int, tasks: Sequence[Task]) -> Sequence[TaskDetail]: ...
    def classify_recurrence(self, series: TaskSeries | None) -> TaskRecurrence: ...

def normalize_task_title(title: str) -> str: ...
def validate_task_due_at(due_at: datetime) -> None: ...
def validate_recurrence_interval(
    recurrence: TaskRecurrence | None, interval_days: int | None
) -> None: ...
```

- [ ] **Step 1: Write focused failing tests**

Create `tests/test_task_references.py` with tests for missing/archived project,
deduplicated/unknown labels, UTC fallback for an unset user timezone, invalid
stored timezone, and invalid named timezone. Create
`tests/test_task_details.py` with tests that a detail includes labels,
reminders, project and recurrence, and that a list detail batch uses
`list_task_labels_for_tasks`/`list_task_reminders_for_tasks` exactly once for
multiple tasks.

Use `FakeTaskManagementRepository` and `fixed_now` from
`tests/task_management_fakes.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_references.py tests/test_task_details.py -q`
Expected: FAIL with missing modules.

- [ ] **Step 3: Implement the collaborators and rewire services**

Move the bodies of `TaskService._validate_project`, `_validate_labels`,
`_user_timezone`, `_normalize_title`, `_validate_due_at`, and
`_validate_recurrence_interval` into the new modules. Keep the exact error
messages. `TaskReferences.named_timezone` is the former
`SeriesService._timezone`.

Move `TaskService._detail`, `_details`, and `_classify_recurrence` into
`TaskDetails`.

Change constructors:

```python
class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        series_repository: SeriesRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        details: TaskDetails,
        series_operations: SeriesOperations,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...
```

`TaskService.today_progress` calls `project_reader.today_progress` directly.
`TaskService` keeps `list`, `today_progress`, `get`, `create`, `update`,
`complete`, `uncomplete`, and `delete` for this task, so it still needs
`series_repository`/`series_operations` until Task 7 moves those methods
out.

```python
class SeriesService:
    def __init__(
        self,
        repository: SeriesRepository,
        task_repository: TaskRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...
```

`SeriesService` keeps `project_reader` until Task 8 moves
`_generate_series`/`_instance_reminder` into `SeriesMaterializer`.
`SeriesService` keeps its own `_normalize_title`, `_validate_time_of_day`,
`_validate_range`, and `_resolve_schedule`, because task and series public
messages/rules differ. Replace `_validate_project`, `_validate_labels`, and
`_timezone` calls with `references`.

Update `composition/task_management.py` and the test factories
(`make_service`, `make_series_service`, `RecordingSeriesService`,
`make_services`) to construct the new collaborators over the same fake
repository.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_task_references.py tests/test_task_details.py tests/test_task_management_tasks.py tests/test_task_management_series.py tests/test_task_management_integration.py tests/test_cross_user_access.py -q`
Expected: PASS with unchanged behavior.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/application/task_management/task_references.py \
  src/backend/application/task_management/task_details.py \
  src/backend/application/task_management/task_inputs.py \
  src/backend/application/task_management/tasks.py \
  src/backend/application/task_management/series.py \
  src/backend/composition/task_management.py \
  tests/test_task_references.py tests/test_task_details.py \
  tests/test_task_management_tasks.py tests/test_task_management_series.py \
  tests/test_task_management_integration.py
git commit -m "refactor: extract shared task references and detail assembly"
```

---

### Task 7: Extract TaskMutationService (update, uncomplete, delete)

**Files:**
- Create: `src/backend/application/task_management/task_mutations.py`
- Modify: `src/backend/application/task_management/tasks.py:315-884,886-1142,1255-1283,1433-1511`
- Modify: `src/backend/composition/task_management.py`
- Modify: `src/backend/api/deps.py`
- Modify: `src/backend/api/routes/tasks.py` (update/uncomplete/delete handlers)
- Test: `tests/test_task_management_tasks.py` (all `.update(`, `.uncomplete(`, `.delete(` call sites)
- Test: `tests/test_tasks_api.py`

**Interfaces:**
- Consumes: `TaskRepository`, `SeriesRepository`, `ReminderRepository`, `TaskReferences`, `TaskDetails`, `SeriesOperations`, `UnitOfWork`, clock.
- Produces: `TaskMutationService` with `update`, `uncomplete`, and `delete`; `TaskService` drops `SeriesRepository`/`SeriesOperations` and keeps only `list`, `today_progress`, `get`, `create`, and `complete`.

```python
class TaskMutationService:
    def __init__(
        self,
        task_repository: TaskRepository,
        series_repository: SeriesRepository,
        reminder_repository: ReminderRepository,
        references: TaskReferences,
        details: TaskDetails,
        series_operations: SeriesOperations,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...
    async def update(
        self,
        user_id: int,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None | UnsetType = UNSET,
        priority: Priority | None = None,
        due_at: datetime | None = None,
        project_id: int | None | UnsetType = UNSET,
        label_ids: Sequence[int] | None = None,
        reminders_enabled: bool | None = None,
        reminders: Sequence[ReminderInput] | None = None,
        recurrence: TaskRecurrence | None = None,
        interval_days: int | None = None,
    ) -> TaskDetail: ...
    async def uncomplete(self, user_id: int, task_id: int) -> TaskDetail: ...
    async def delete(self, user_id: int, task_id: int) -> None: ...
```

- [ ] **Step 1: Update the test fixtures to require the new owner**

In `tests/test_task_management_tasks.py`, add
`make_mutation_service(repository, *, series_operations=None, clock=fixed_now)`
that constructs `TaskMutationService` with the same fake repository,
`TaskReferences`, `TaskDetails`, and `RecordingSeriesService`. Replace
`service.update(`, `service.uncomplete(`, and `service.delete(` with
`mutation_service.*` in this file; keep the expected results and exception
assertions unchanged. Update `make_service` and `RecordingSeriesService` so
`TaskService` no longer receives `series_repository`/`series_operations`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py -q`
Expected: FAIL with missing `TaskMutationService` and the updated `TaskService`
constructor.

- [ ] **Step 3: Move all mutation methods and update wiring**

Create `task_mutations.py` and move `update`, `uncomplete`, `delete`,
`_update_detached_task`, `_apply_task_update`, `_lock_update_target`,
`_update_series_task`, `_shift_overdue_task`, and `_rebuild_task_reminders`
from `TaskService`. Use `details.detail` instead of `_detail`, `references`
instead of the moved validators, and `normalize_task_title`/
`validate_task_due_at`/`validate_recurrence_interval` from `task_inputs`.
Preserve the `update` keyword-only signature exactly.

Remove those methods from `TaskService`; keep `_require_task` for
`get`/`complete`. Preserve the `ReminderConfigurationError -> BadRequestError`
translation that Task 3 added to `update`, `uncomplete`, and
`_rebuild_task_reminders`. Change `TaskService` dependencies to:

```python
class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        details: TaskDetails,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...
```

Add `TaskMutationService` to `TaskManagementServices` and
`build_task_management_services`. Add `TaskMutationServiceDep` in
`api/deps.py`. Update the `PATCH /tasks/{task_id}`,
`POST /tasks/{task_id}/uncomplete`, and `DELETE /tasks/{task_id}` routes to
depend on it; keep `POST /tasks/{task_id}/complete` on `TaskServiceDep`.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_task_management_tasks.py tests/test_tasks_api.py tests/test_composition.py tests/test_deps.py tests/test_cross_user_access.py -q`
Expected: PASS with unchanged HTTP responses, transaction calls, and
series-before-task lock order.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/application/task_management/task_mutations.py \
  src/backend/application/task_management/tasks.py \
  src/backend/composition/task_management.py src/backend/api/deps.py \
  src/backend/api/routes/tasks.py tests/test_task_management_tasks.py \
  tests/test_tasks_api.py
git commit -m "refactor: extract task mutation use cases"
```

---

### Task 8: Extract SeriesMaterializer and shared instance-reminder helper

**Files:**
- Create: `src/backend/application/task_management/series_reminders.py`
- Create: `src/backend/application/task_management/series_materializer.py`
- Modify: `src/backend/application/task_management/series.py:182-400,704-1060`
- Modify: `src/backend/composition/task_management.py`
- Test: `tests/test_series_materializer.py` (create)
- Test: `tests/test_task_management_series.py`
- Test: `tests/test_task_management_integration.py`

**Interfaces:**
- Consumes: `SeriesRepository`, `TaskRepository`, `ReminderRepository`, `ProjectReader`, `TaskReferences`, `UnitOfWork`.
- Produces:

```python
def build_instance_reminder(
    template: TaskReminder,
    *,
    task_id: int,
    due_at: datetime,
    fallback_timezone: str,
    is_periodic: bool,
    now: datetime,
) -> TaskReminder | None: ...

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

- [ ] **Step 1: Write the failing materializer test**

Create `tests/test_series_materializer.py`:

```python
async def test_materializer_creates_tasks_labels_and_instance_reminders() -> None:
    """One series materializes the same tasks, label rows, and reminders as before."""
    repository = FakeTaskManagementRepository()
    service = make_series_service(repository)
    series = await service.create(...)  # same fixture values as the existing create test
    assert series.generated_through is not None
    assert any(task.series_id == series.id for task in repository.tasks.values())
```

Also add a test that an archived project yields zero materialized tasks and
no writes, and a test that `build_instance_reminder` matches the existing
before-due/daily/periodic slot rules.

- [ ] **Step 2: Run the test to verify it fails**

Run: `poetry run pytest tests/test_series_materializer.py -q`
Expected: FAIL with missing modules.

- [ ] **Step 3: Move the generation helpers**

Move `_generate_series` to `SeriesMaterializer.materialize`; move
`_instance_reminder` to `build_instance_reminder`. Preserve the
`ReminderConfigurationError -> BadRequestError` translation from Task 3.
Keep the exact order: project-active check, `series_due_times`, existing-slot
dedupe, series labels, template reminders, bulk `add_tasks`, label rows,
instance reminders, `generated_through` save, and flush-only-when-no-task-rows.

Replace the `SeriesService._generate_series` calls in `create`,
`create_from_task`, and `update` with `self._materializer.materialize`.
Update `_update_future_instances` to call `build_instance_reminder`.

Change `SeriesService` dependencies to:

```python
class SeriesService:
    def __init__(
        self,
        repository: SeriesRepository,
        task_repository: TaskRepository,
        reminder_repository: ReminderRepository,
        references: TaskReferences,
        materializer: SeriesMaterializer,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...
```

Update `composition/task_management.py` and the series test factories.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_series_materializer.py tests/test_task_management_series.py tests/test_task_management_integration.py tests/test_composition.py -q`
Expected: PASS, including flush-before-dedupe and generation assertions.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/application/task_management/series_reminders.py \
  src/backend/application/task_management/series_materializer.py \
  src/backend/application/task_management/series.py \
  src/backend/composition/task_management.py \
  tests/test_series_materializer.py tests/test_task_management_series.py \
  tests/test_task_management_integration.py
git commit -m "refactor: extract series materializer"
```

---

### Task 9: Extract SeriesGenerationService

**Files:**
- Create: `src/backend/application/task_management/series_generation.py`
- Modify: `src/backend/application/task_management/series.py:704-773`
- Modify: `src/backend/composition/task_management.py`
- Modify: `src/backend/tasks/generation.py`
- Test: `tests/test_task_generation.py`
- Test: `tests/test_task_management_series.py` (all `.generate(` calls)
- Test: `tests/test_task_management_integration.py:203-218`
- Test: `tests/test_composition.py`

**Interfaces:**
- Consumes: `SeriesRepository`, `SeriesMaterializer`, `UnitOfWork`, clock.
- Produces:

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

- [ ] **Step 1: Update generation tests to the new owner**

In `tests/test_task_generation.py`, replace the monkeypatched
`build_series_service` with `build_series_generation_service` and import the
real factory. In `tests/test_task_management_series.py` and
`tests/test_task_management_integration.py`, build
`SeriesGenerationService` over the same fake repository and replace
`service.generate(` with `generation_service.generate(`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_generation.py tests/test_task_management_series.py -q`
Expected: FAIL with missing `SeriesGenerationService`/factory.

- [ ] **Step 3: Move the generation loop**

Move `generate` to `SeriesGenerationService`, preserving:
`list_series_for_generation`, candidate-id snapshot, `claim_series_for_generation`,
`generated_through >= horizon` skip, project-active handled by materializer,
per-series `materialize`, `commit`, and rollback/continue.

Add `build_series_generation_service(session, *, clock=None)` to
`composition/task_management.py`. Update `tasks/generation.py` to call it.
Remove `generate` from `SeriesService` and drop the now-unused imports/logger.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_task_generation.py tests/test_task_management_series.py tests/test_task_management_integration.py tests/test_composition.py -q`
Expected: PASS, including per-series commit/rollback and candidate snapshot assertions.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/application/task_management/series_generation.py \
  src/backend/application/task_management/series.py \
  src/backend/composition/task_management.py src/backend/tasks/generation.py \
  tests/test_task_generation.py tests/test_task_management_series.py \
  tests/test_task_management_integration.py tests/test_composition.py
git commit -m "refactor: extract series generation service"
```

---

### Task 10: Move project reminder resume policy into application

**Files:**
- Modify: `src/backend/domain/task_management/models.py`
- Modify: `src/backend/application/ports/task_management.py:1640-1695`
- Modify: `src/backend/application/ports/collaborators.py`
- Create: `src/backend/application/task_management/project_reminders.py`
- Modify: `src/backend/application/task_management/projects.py:28-42,341-383,385-412`
- Modify: `src/backend/infrastructure/persistence/sqlalchemy/reminder_repository.py:393-490`
- Modify: `src/backend/composition/task_management.py`
- Test: `tests/test_task_management_projects.py`
- Test: `tests/test_task_management_reminders.py`
- Test: `tests/test_port_contracts.py`
- Test: `tests/task_management_fakes.py`

**Interfaces:**
- Consumes: `ReminderRepository.list_project_reminder_contexts`, `save_reminders`, `pause_project_reminders`.
- Produces:

```python
@dataclass(frozen=True)
class ProjectReminderContext:
    reminder: TaskReminder
    task_due_at: datetime | None
    series_timezone: str | None
    is_series: bool

class ProjectReminderCommands(Protocol):
    async def pause_project(self, user_id: int, project_id: int) -> None: ...
    async def resume_project(
        self, user_id: int, project_id: int, now: datetime
    ) -> None: ...

class ProjectReminderService:
    def __init__(self, repository: ReminderRepository) -> None: ...
```

- [ ] **Step 1: Write the failing contract and behavior tests**

In `tests/test_port_contracts.py`, assert `ProjectReminderCommands` exposes
exactly `pause_project`/`resume_project`, `ReminderRepository` exposes
`list_project_reminder_contexts`/`save_reminders`, and no longer exposes
`resume_project_reminders`. Update `AGGREGATE_METHODS`/`ALL_AGGREGATE_METHODS`
so the exact-method and partition tests describe the new reminder contract.

Add to `tests/test_task_management_projects.py` or a new
`tests/test_project_reminders.py`:

```python
async def test_resume_project_recomputes_task_slots_and_keeps_templates_empty() -> None:
    """Unarchive recomputes task reminders through application policy."""
    repository = FakeTaskManagementRepository()
    # seed archived project, task reminder, and series template reminder
    service = ProjectReminderService(repository)
    await service.resume_project(USER_ID, project.id, fixed_now())
    assert task_reminder.next_run_at == expected_slot
    assert series_template.next_run_at is None
    assert repository.save_reminder_batches == 1
```

Add a test that `pause_project` keeps the bulk repository call and does not
touch reminder slots itself.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_port_contracts.py tests/test_task_management_projects.py -q`
Expected: FAIL with missing context/commands/service and the old
`resume_project_reminders` method.

- [ ] **Step 3: Implement the context reader and application policy**

Add `ProjectReminderContext` to domain models. In the SQLAlchemy adapter,
replace the mutating `resume_project_reminders` with
`list_project_reminder_contexts`: run the same four batch queries, map rows to
domain entities, and return contexts without mutating or flushing. Add
`save_reminders` that applies all changes and calls `flush` once.

In the fake repository, remove `resume_project_reminders`; implement
`list_project_reminder_contexts`, `save_reminders`, and keep
`pause_project_reminders`.

Implement `ProjectReminderService`:

- `pause_project` calls `repository.pause_project_reminders`;
- `resume_project` loads contexts, sets series templates to
  `next_run_at=None` and retargets scheduled template timezones, recomputes
  task slots with `rebuild_reminder_slot` (catching
  `ZoneInfoNotFoundError`/`ValueError` and storing `None`), then calls
  `save_reminders` once.

Change `ProjectService` to depend on `ProjectReminderCommands`; update
`unarchive` to call `resume_project`, and `archive` to call `pause_project`.
Update composition wiring to pass `ProjectReminderService`. Update the
project test fixture and `tests/task_management_fakes.py`.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_task_management_projects.py tests/test_task_management_reminders.py tests/test_port_contracts.py tests/test_composition.py tests/test_task_management_integration.py -q`
Expected: PASS with the same reminder slots, four context queries, and one
batch save flush.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/domain/task_management/models.py \
  src/backend/application/ports/task_management.py \
  src/backend/application/ports/collaborators.py \
  src/backend/application/task_management/project_reminders.py \
  src/backend/application/task_management/projects.py \
  src/backend/infrastructure/persistence/sqlalchemy/reminder_repository.py \
  src/backend/composition/task_management.py \
  tests/task_management_fakes.py tests/test_task_management_projects.py \
  tests/test_task_management_reminders.py tests/test_port_contracts.py \
  tests/test_composition.py tests/test_task_management_integration.py
git commit -m "refactor: move project reminder resume policy to application"
```

---

### Task 11: Unify the composition graph and adapter builder

**Files:**
- Modify: `src/backend/composition/task_management.py`
- Modify: `src/backend/api/deps.py`
- Modify: `src/backend/api/routes/tasks.py` (only if dependency names change)
- Test: `tests/test_composition.py`
- Test: `tests/test_deps.py`
- Test: `tests/test_task_management_integration.py`

**Interfaces:**
- Consumes: all aggregate adapters, `SqlAlchemyUnitOfWork`, new application services.
- Produces:

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

def _build_task_management_adapters(session: AsyncSession) -> TaskManagementAdapters: ...
```

`TaskManagementServices` adds `task_mutations: TaskMutationService`.

- [ ] **Step 1: Write the failing single-graph test**

Add to `tests/test_composition.py`:

```python
async def test_api_services_share_one_request_graph() -> None:
    """All task-management services come from one cached, shared graph."""
    session = make_session()
    services = await get_task_management_services(session)
    projects = await get_project_service(services)
    labels = await get_label_service(services)
    series = await get_series_service(services)
    tasks = await get_task_service(services)
    mutations = await get_task_mutation_service(services)

    assert cast(Any, projects)._repository is cast(Any, services.projects)._repository
    assert cast(Any, labels)._repository is cast(Any, services.labels)._repository
    assert cast(Any, series) is services.series
    assert cast(Any, tasks) is services.tasks
    assert cast(Any, mutations) is services.task_mutations
    assert cast(Any, tasks)._unit_of_work is cast(Any, services.task_mutations)._unit_of_work
```

Also assert `build_series_generation_service`, `build_reminder_dispatcher`, and
`build_task_management_services` each bind every adapter to the supplied
session.

- [ ] **Step 2: Run the test to verify it fails**

Run: `poetry run pytest tests/test_composition.py::test_api_services_share_one_request_graph -q`
Expected: FAIL with missing `TaskManagementAdapters`/dependency.

- [ ] **Step 3: Implement the adapter builder and single graph**

Add `TaskManagementAdapters` and `_build_task_management_adapters(session)`.
Refactor `build_task_management_services`, `build_series_service`,
`build_series_generation_service`, `build_reminder_dispatcher`, and
`build_project_reminder_service` to consume it.

Add `get_task_management_services(session)` to `api/deps.py` and derive
`get_task_service`, `get_task_mutation_service`, `get_series_service`,
`get_project_service`, `get_label_service`, and `get_palette_service` from
the `TaskManagementServices` dependency. FastAPI caches the graph per request.
Keep `build_palette_service` for admin.

Update tests that call `get_*_service` directly to pass the graph or keep
zero-argument wrappers where FastAPI resolves the dependency.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_composition.py tests/test_deps.py tests/test_tasks_api.py tests/test_projects_api.py tests/test_labels_api.py tests/test_task_series_api.py -q`
Expected: PASS with one graph per request.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/composition/task_management.py src/backend/api/deps.py \
  src/backend/api/routes/tasks.py tests/test_composition.py tests/test_deps.py \
  tests/test_task_management_integration.py
git commit -m "refactor: unify task management composition graph"
```

---

### Task 12: Batch palette lookup and remove passthrough project helper

**Files:**
- Modify: `src/backend/api/routes/labels.py:29-70`
- Modify: `src/backend/api/routes/projects.py:44-65` and call sites
- Test: `tests/test_labels_api.py`
- Test: `tests/test_projects_api.py`

**Interfaces:**
- Consumes: `PaletteService.get_colors` and `ProjectService.get_summary`.
- Produces: unchanged HTTP responses.

- [ ] **Step 1: Write the failing batch-palette test**

Add to `tests/test_labels_api.py` a test with two labels sharing one color and
a third label with a second color; monkeypatch/spy `PaletteService.get_colors`
and assert it is called once with both color IDs, while
`PaletteService.get_color` is not called.

Add a project route test that `_load_summary` is gone (assert the module has no
`_load_summary` attribute) and that the existing project response is unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_labels_api.py tests/test_projects_api.py -q`
Expected: FAIL because the label route resolves colors one by one and
`_load_summary` still exists.

- [ ] **Step 3: Implement the API cleanups**

In `list_labels`, call `palette_service.get_colors(sorted(color_ids))` once and
build responses from the returned mapping. Remove the per-label cache loop.
Remove `_load_summary` from `api/routes/projects.py` and call
`service.get_summary` directly in the read/mutation handlers.

- [ ] **Step 4: Run target tests and the full suite**

Run: `poetry run pytest tests/test_labels_api.py tests/test_projects_api.py tests/test_cross_user_access.py -q`
Expected: PASS with identical JSON.

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/api/routes/labels.py src/backend/api/routes/projects.py \
  tests/test_labels_api.py tests/test_projects_api.py
git commit -m "refactor: batch label palette lookup and remove passthrough"
```

---

### Task 13: Final architecture guards, documentation, and verification

**Files:**
- Modify: `tests/test_architecture.py`
- Modify: `README.md:11-55`
- Modify: `AGENTS.md` (compatibility/composition wording only)
- Modify: `graphify-out/` via `graphify update .`

**Interfaces:**
- Consumes: the completed refactor.
- Produces: CI guards that keep the new boundaries and services from regressing.

- [ ] **Step 1: Write the final guard tests**

Add these tests to `tests/test_architecture.py`:

```python
def test_domain_does_not_import_core_errors() -> None: ...  # already added in Task 3
def test_application_does_not_import_concrete_adapters() -> None:
    """Application depends only on ports, domain, and shared kernel errors."""
    for path in Path("src/backend/application").rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith("backend.infrastructure"), f"{path}: {imported}"
            assert not imported.startswith("backend.composition"), f"{path}: {imported}"


def test_infrastructure_does_not_import_the_composition_root() -> None:
    """Adapters never reach outward to the module that chooses them."""
    for path in Path("src/backend/infrastructure").rglob("*.py"):
        for imported in _imports(path):
            assert not imported.startswith("backend.composition"), f"{path}: {imported}"


def test_api_does_not_import_concrete_infrastructure() -> None: ...  # already added in Task 5


def test_removed_legacy_composite_modules_are_absent() -> None: ...  # already added in Task 1
```

Keep `test_domain_and_application_do_not_import_outer_layers` and
`test_api_does_not_import_orm_models` as-is.

- [ ] **Step 2: Update documentation**

In `README.md`, remove the statement that
`task_management/repository.py` re-exports an infrastructure adapter; mention
that only `registry`-style compatibility re-exports remain and that the
composite legacy facade was removed. In `AGENTS.md`, keep the layer rules and
update only wording that describes removed compatibility modules; do not change
behavioral rules.

- [ ] **Step 3: Run the final verification suite**

Run:

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git status --short
```

Expected: every command exits 0; `git status --short` shows only intended
source/test/docs/graphify changes, no generated database or environment files.

- [ ] **Step 4: Inspect and commit the final task**

```bash
git diff --stat
git add README.md AGENTS.md tests/test_architecture.py graphify-out
git commit -m "chore: add architecture guards and refresh docs"
```

Do not stage unrelated pre-existing working-tree changes.
