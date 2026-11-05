# Task Recurrence Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `one_off` / `regular` / `periodic` recurrence switching for tasks, periodic series with `interval_days >= 2`, phase-preserving generation, detach semantics, and the new API contract.

**Architecture:** Extend the existing task-management domain instead of adding a parallel model. `task_series` gains `interval_days`; scheduling generalizes to phase-anchored `series_due_times`; `SeriesService` owns create/update/create_from_task/detach; `TaskService` chooses the toggle branch, shifts overdue due times, and applies the new DELETE matrix. The repository gains deleted-series reads and physical deletion scoped to the user, while the fake repository mirrors every change.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL, Alembic, pytest, zoneinfo.

**Spec:** `docs/superpowers/specs/2026-11-04-task-recurrence-toggle-design.md`

## Global Constraints

- No new runtime or dev dependencies.
- Recurrence values are exactly `one_off`, `regular`, `periodic`.
- `regular` stores `frequency = 'daily'`, `interval_days = 1`.
- `periodic` stores `frequency = 'periodic'`, `interval_days >= 2`.
- `one_off` stores `tasks.series_id IS NULL`.
- The new migration is `0005_periodic_task_series`; `0004_task_series_generation_index` already exists and must remain unchanged.
- `task_series.interval_days` is `SMALLINT NOT NULL DEFAULT 1`.
- Check constraints: `frequency IN ('daily','periodic')`, `interval_days >= 1`, and `(daily AND interval_days = 1) OR (periodic AND interval_days >= 2)`.
- All due times remain aware UTC instants; local date/time arithmetic uses `zoneinfo`.
- Generation horizon remains 90 days; past occurrences are never backfilled.
- History is preserved: completed and overdue instances are never rewritten by toggles, except the current task's overdue shift.
- `one_off -> series` uses the task's author timezone and snapshots labels/reminders.
- `series -> one_off` physically deletes only future incomplete siblings, excluding the current task.
- A soft-deleted series is history: its tasks still expose `regular`/`periodic`, and edits/deletes treat them as one-off tasks.
- `POST /api/v1/tasks` still creates only one-off tasks.
- Every new or changed function/class gets an English docstring in the project template; update stale docstrings in the same change.
- Keep the existing flat error envelope and `ErrorCode` usage. Use `BadRequestError` / `NotFoundError`; do not invent string error codes.
- Do not stage unrelated pre-existing working-tree changes.
- Verify after each task: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`.
- After the final task: `graphify update .`.

## Review Focus

- Overdue toggle moves strictly after today's local day, preserves local wall time, and uses the correct anchor (`task` date when enabling, `series.starts_on` when disabling).
- Detach hard-deletes only future incomplete siblings except the current task; completed/overdue history remains.
- DELETE distinguishes active future series tasks from completed/overdue ones; only the active future case deletes the series.
- Periodic generation preserves the `starts_on + k * interval_days` phase across `generated_through`, `ends_on`, the horizon, and DST.
- API validation is exact: `one_off` rejects `interval_days`, `periodic` requires `>= 2`, and interval-only updates are allowed only on existing periodic series.

---

### Task 1: Recurrence enums and phase-aware scheduling

**Files:**
- Modify: `src/backend/task_management/enums.py`
- Modify: `src/backend/task_management/schedule.py`
- Test: `tests/test_task_management_schedule.py`

**Interfaces:**
- Consumes: existing `localize_wall_time`, `HORIZON_DAYS`.
- Produces:
  - `SeriesFrequency.PERIODIC = "periodic"`.
  - `TaskRecurrence` (`StrEnum`): `ONE_OFF = "one_off"`, `REGULAR = "regular"`, `PERIODIC = "periodic"`.
  - `series_due_times(*, starts_on: date, ends_on: date | None, time_of_day: time, timezone: ZoneInfo, now: datetime, interval_days: int = 1, generated_through: datetime | None = None, horizon_days: int = HORIZON_DAYS) -> list[datetime]`.
  - `next_occurrence_after_date(*, anchor: date, interval_days: int, time_of_day: time, timezone: ZoneInfo, now: datetime) -> datetime`.
- Removes production/test use of `daily_due_times`; keep `next_daily_time` unchanged unless a test explicitly needs it.

- [ ] **Step 1: Write the failing scheduling tests**

Add to `tests/test_task_management_schedule.py` (and rename existing `daily_due_times` calls to `series_due_times`):

```python
from backend.task_management.enums import TaskRecurrence
from backend.task_management.schedule import next_occurrence_after_date, series_due_times


def test_series_due_times_periodic_uses_phase_and_skips_passed_slot() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
    )
    assert result[0] == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    assert result[1] == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


def test_series_due_times_periodic_resume_keeps_anchor_phase() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=date(2026, 11, 16),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
        generated_through=datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
    )
    assert result == [
        datetime(2026, 11, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 13, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 16, 6, 0, tzinfo=UTC),
    ]


def test_series_due_times_periodic_future_start_beyond_horizon_returns_one() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2027, 6, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
    )
    assert result == [datetime(2027, 6, 1, 6, 0, tzinfo=UTC)]


def test_next_occurrence_after_date_is_strictly_after_today() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert next_occurrence_after_date(
        anchor=date(2026, 11, 4),
        interval_days=1,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    ) == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    assert next_occurrence_after_date(
        anchor=date(2026, 11, 1),
        interval_days=3,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    ) == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("interval_days", [0, -1])
def test_periodic_helpers_reject_non_positive_interval(interval_days: int) -> None:
    with pytest.raises(ValueError):
        series_due_times(
            starts_on=date(2026, 11, 1),
            ends_on=None,
            time_of_day=time(9, 0),
            timezone=MOSCOW,
            now=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
            interval_days=interval_days,
        )
```

Also add:
- `test_series_due_times_periodic_stops_at_ends_on_inclusive` with `starts_on=2026-11-01`, `ends_on=2026-11-10`, `N=3`, `now=2026-11-04 12:00 UTC`, expected `11-07`, `11-10`.
- `test_series_due_times_periodic_respects_horizon`.
- `test_series_due_times_periodic_spring_forward_clamps` using `Europe/Berlin`, `time(2, 30)`, phase date `2026-03-29`, `N=1`.
- `test_next_occurrence_after_date_periodic_skips_multiple_passed_slots` with anchor `2026-10-20`, `N=3`, now `2026-11-04`, expected `2026-11-07` local date (phase dates: 10-20, 10-23, 10-26, 10-29, 11-01, 11-04, 11-07).
- `test_task_recurrence_values`.

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
poetry run pytest tests/test_task_management_schedule.py -q
```

Expected: import errors for `series_due_times`, `next_occurrence_after_date`, or `TaskRecurrence`.

- [ ] **Step 3: Implement the enums and scheduling helpers**

In `enums.py` add `PERIODIC` to `SeriesFrequency` and add `TaskRecurrence`.

In `schedule.py`:
- rename `daily_due_times` to `series_due_times` and add `interval_days: int = 1`;
- raise `ValueError("interval_days must be at least 1")` when `< 1`;
- compute the first phase date with an integer-aligned helper:

```python
def _phase_on_or_after(anchor: date, target: date, interval_days: int) -> date:
    if target <= anchor:
        return anchor
    steps = ((target - anchor).days + interval_days - 1) // interval_days
    return anchor + timedelta(days=steps * interval_days)
```

- when `generated_through` is present, use `generated_local_date + 1 day` as the lower target;
- start from `_phase_on_or_after(starts_on, max(lower_target, local_now.date()), interval_days)`;
- if the localized candidate is `<= now`, advance by one interval;
- advance each loop iteration by `interval_days`;
- keep the existing horizon and `ends_on` behavior;
- implement `next_occurrence_after_date` with the same phase arithmetic:
  - if `anchor > local_today`, return `localize_wall_time(anchor, ...)`;
  - otherwise `steps = (local_today - anchor).days // interval_days + 1`;
  - return `localize_wall_time(anchor + timedelta(days=steps * interval_days), ...)`.
- update docstrings with `Purpose`, `Args`, `Returns`, `Raises`, and the phase/DST notes.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_schedule.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/task_management/enums.py src/backend/task_management/schedule.py tests/test_task_management_schedule.py
git commit -m "feat: add phase-aware periodic scheduling"
```

---

### Task 2: Series interval model and Alembic migration

**Files:**
- Modify: `src/backend/models/task_series.py`
- Create: `alembic/versions/0005_periodic_task_series.py`
- Test: `tests/test_task_management_models.py`
- Test: `tests/test_task_management_migration.py`

**Interfaces:**
- Consumes: `SeriesFrequency.PERIODIC` from Task 1.
- Produces:
  - `TaskSeries.interval_days: Mapped[int]` (`SmallInteger`, non-null, server default `1`, Python default `1`).
  - ORM constraints `ck_task_series_frequency`, `ck_task_series_interval_days_positive`, `ck_task_series_frequency_interval`.
  - Alembic revision `0005_periodic_task_series`, `down_revision = "0004_task_series_generation_index"`.

- [ ] **Step 1: Write the failing model and migration tests**

In `tests/test_task_management_models.py`, update `test_check_constraints_are_declared`:

```python
"task_series": {
    "ck_task_series_frequency",
    "ck_task_series_interval_days_positive",
    "ck_task_series_frequency_interval",
    "ck_task_series_ends_on_after_starts_on",
    "ck_task_series_priority",
},
```

Add:

```python
def test_task_series_interval_days_column_and_checks() -> None:
    table = Base.metadata.tables["task_series"]
    assert isinstance(table.c.interval_days.type, SmallInteger)
    assert table.c.interval_days.nullable is False
    assert table.c.interval_days.server_default is not None
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "interval_days >= 1" in checks["ck_task_series_interval_days_positive"]
    assert "frequency = 'daily'" in checks["ck_task_series_frequency_interval"]
    assert "frequency = 'periodic'" in checks["ck_task_series_frequency_interval"]
    assert "ck_task_series_frequency_daily" not in checks
```

In `tests/test_task_management_migration.py` add a loader and tests:

```python
def _load_periodic_migration() -> ModuleType:
    path = Path("alembic/versions/0005_periodic_task_series.py")
    spec = importlib.util.spec_from_file_location("migration_0005", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_periodic_migration_chain() -> None:
    migration = _load_periodic_migration()
    assert migration.revision == "0005_periodic_task_series"
    assert migration.down_revision == "0004_task_series_generation_index"


def test_periodic_migration_upgrade_and_downgrade_operations(monkeypatch) -> None:
    migration = _load_periodic_migration()
    calls: list[tuple[object, ...]] = []

    class FakeOp:
        def add_column(self, *args: object, **kwargs: object) -> None:
            calls.append(("add_column", args, kwargs))

        def drop_constraint(self, *args: object, **kwargs: object) -> None:
            calls.append(("drop_constraint", args, kwargs))

        def create_check_constraint(self, *args: object, **kwargs: object) -> None:
            calls.append(("create_check_constraint", args, kwargs))

        def drop_column(self, *args: object, **kwargs: object) -> None:
            calls.append(("drop_column", args, kwargs))

        def execute(self, *args: object, **kwargs: object) -> None:
            calls.append(("execute", args, kwargs))

    monkeypatch.setattr(migration, "op", FakeOp())
    migration.upgrade()
    assert any(call[0] == "add_column" for call in calls)
    assert any(call[0] == "create_check_constraint" for call in calls)
    calls.clear()
    migration.downgrade()
    assert any(call[0] == "drop_column" for call in calls)
    assert any(call[0] == "execute" for call in calls)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_task_management_models.py tests/test_task_management_migration.py -q
```

Expected: FAIL because `interval_days` and the migration file do not exist.

- [ ] **Step 3: Implement the model and migration**

`TaskSeries`:
- import `SmallInteger`;
- add `interval_days` after `frequency`;
- replace `frequency_daily` with `interval_days_positive` and `frequency_interval`;
- update the class docstring to mention daily/periodic series.

Migration `0005_periodic_task_series.py`:
- `upgrade()`: add the column with `server_default=sa.text("1")`, drop the two old constraints, create the three new ones;
- `downgrade()`: run
  `UPDATE task_series SET frequency = 'daily', interval_days = 1 WHERE frequency = 'periodic'`,
  drop the new constraints, drop `interval_days`, recreate the old daily-only constraints;
- docstrings on `upgrade`/`downgrade` with `Purpose` and destructive-downgrade note.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_models.py tests/test_task_management_migration.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/models/task_series.py alembic/versions/0005_periodic_task_series.py tests/test_task_management_models.py tests/test_task_management_migration.py
git commit -m "feat: store periodic series intervals"
```

---

### Task 3: Repository, ports, and fake parity

**Files:**
- Modify: `src/backend/task_management/ports.py`
- Modify: `src/backend/task_management/repository.py`
- Modify: `tests/task_management_fakes.py`
- Test: `tests/test_task_management_repository.py`

**Interfaces:**
- Consumes: `TaskSeries.interval_days` from Task 2.
- Produces:
  - `get_series(..., include_deleted: bool = False) -> TaskSeries | None`.
  - `get_series_by_ids(user_id: int, series_ids: Sequence[int], *, include_deleted: bool = False) -> dict[int, TaskSeries]`.
  - `hard_delete_tasks(task_ids: Sequence[int], user_id: int) -> None`.
  - `list_tasks` recurrence filter using a `task_series` join and `frequency`.
  - Fake repository methods with identical behavior, including cascade-like cleanup.
  - `make_series(..., frequency: str = "daily", interval_days: int = 1)`.

- [ ] **Step 1: Write the failing repository tests**

Add to `tests/test_task_management_repository.py`:

```python
async def test_list_tasks_regular_joins_daily_series_without_deleted_filter() -> None:
    session = RecordingSession()
    repository = make_repository(session)
    await repository.list_tasks(
        USER_ID,
        date_start=None,
        date_end=None,
        project_ids=(),
        priorities=(),
        status="all",
        recurrence="regular",
        query=None,
        include_archived_projects=False,
        now=NOW,
        limit=50,
        after_due_at=None,
        after_task_id=None,
    )
    sql = compile_statement(session.statements[-1])
    assert "JOIN task_series" in sql
    assert "task_series.frequency = 'daily'" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_list_tasks_periodic_joins_periodic_series() -> None:
    session = RecordingSession()
    repository = make_repository(session)
    await repository.list_tasks(
        USER_ID,
        date_start=None,
        date_end=None,
        project_ids=(),
        priorities=(),
        status="all",
        recurrence="periodic",
        query=None,
        include_archived_projects=False,
        now=NOW,
        limit=50,
        after_due_at=None,
        after_task_id=None,
    )
    sql = compile_statement(session.statements[-1])
    assert "JOIN task_series" in sql
    assert "task_series.frequency = 'periodic'" in sql


async def test_get_series_can_include_deleted_rows() -> None:
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_repository(session)
    await repository.get_series(USER_ID, 3, include_deleted=True)
    sql = compile_statement(session.statements[-1])
    assert "task_series.id = 3" in sql
    assert "task_series.user_id = 7" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_get_series_by_ids_is_tenant_scoped_and_optional_deleted() -> None:
    session = RecordingSession()
    repository = make_repository(session)
    await repository.get_series_by_ids(USER_ID, (1, 2), include_deleted=True)
    sql = compile_statement(session.statements[-1])
    assert "task_series.user_id = 7" in sql
    assert "task_series.id IN (1, 2)" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_hard_delete_tasks_is_user_scoped_and_physical() -> None:
    session = RecordingSession()
    repository = make_repository(session)
    await repository.hard_delete_tasks((4, 5), USER_ID)
    sql = compile_statement(session.statements[-1])
    assert sql.startswith("DELETE FROM tasks")
    assert "tasks.id IN (4, 5)" in sql
    assert "tasks.user_id = 7" in sql
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_task_management_repository.py -q
```

Expected: FAIL for missing methods or old recurrence SQL.

- [ ] **Step 3: Implement repository and fake behavior**

Ports:
- add the exact signatures and docstrings from the Interfaces block;
- update `list_tasks` recurrence docs to include `periodic`.

SQL repository:
- `get_series`: add conditional `TaskSeries.deleted_at.is_(None)`;
- `get_series_by_ids`: return `{}` for empty input; select by ids and user; optional deleted filter; return dict;
- `hard_delete_tasks`: return early for empty input; execute `delete(Task).where(Task.id.in_(ids), Task.user_id == user_id)`; flush; rely on FK cascades;
- `list_tasks`: for `regular`/`periodic`, join `TaskSeries` on `and_(TaskSeries.id == Task.series_id, TaskSeries.user_id == user_id)` and filter `frequency`; do not filter series `deleted_at`; keep `one_off` as `Task.series_id.is_(None)`.

Fake:
- `get_series` adds `include_deleted`;
- add `get_series_by_ids`;
- add `hard_delete_tasks` that removes task-label pairs, task reminders, their deliveries, and the tasks;
- `list_tasks` filters `regular`/`periodic` by the referenced series frequency, including soft-deleted series;
- `make_series` adds `frequency` and `interval_days` parameters and passes them to `TaskSeries`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_repository.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/task_management/ports.py src/backend/task_management/repository.py tests/task_management_fakes.py tests/test_task_management_repository.py
git commit -m "feat: support periodic series persistence"
```

---

### Task 4: Series create/update and periodic generation

**Files:**
- Modify: `src/backend/task_management/series.py`
- Test: `tests/test_task_management_series.py`

**Interfaces:**
- Consumes: `series_due_times`, `SeriesFrequency.PERIODIC`, `TaskSeries.interval_days`, repository methods from Task 3.
- Produces:
  - `SeriesService.create(..., frequency: SeriesFrequency = SeriesFrequency.DAILY, interval_days: int | None = None, ...)`.
  - `SeriesService.update(..., frequency: SeriesFrequency | None = None, interval_days: int | None = None, ...)`.
  - `_generate_series` passes `series.interval_days`.
  - Frequency/interval validation returns `BadRequestError` for invalid combinations.

- [ ] **Step 1: Write the failing series tests**

Add to `tests/test_task_management_series.py`:

```python
async def test_create_periodic_series_stores_interval_and_generates_phase_slots() -> None:
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    series = await service.create(
        USER_ID,
        title="Periodic",
        description=None,
        priority=Priority.NEITHER,
        frequency=SeriesFrequency.PERIODIC,
        interval_days=3,
        time_of_day=time(9, 0),
        timezone="Europe/Moscow",
        starts_on=date(2026, 11, 1),
        ends_on=None,
        project_id=None,
        label_ids=(),
        reminders_enabled=False,
        reminders=(),
    )

    assert series.frequency == "periodic"
    assert series.interval_days == 3
    due_times = sorted(task.due_at for task in repository.tasks.values())
    assert due_times[0] == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    assert due_times[1] == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("frequency", "interval_days"),
    [
        (SeriesFrequency.DAILY, 3),
        (SeriesFrequency.PERIODIC, None),
        (SeriesFrequency.PERIODIC, 1),
    ],
)
async def test_create_series_rejects_invalid_frequency_interval(
    frequency: SeriesFrequency,
    interval_days: int | None,
) -> None:
    service = make_service(FakeTaskManagementRepository())
    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Invalid",
            description=None,
            priority=Priority.NEITHER,
            frequency=frequency,
            interval_days=interval_days,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=date(2026, 11, 4),
            ends_on=None,
            project_id=None,
            label_ids=(),
            reminders_enabled=False,
            reminders=(),
        )
```

Add:
- `test_update_periodic_interval_regenerates_future_instances` — existing periodic series with a future instance; `update(interval_days=4)` soft-deletes the old future task and creates new phase slots; past/completed unchanged.
- `test_update_daily_to_periodic_regenerates_future_instances` — `update(frequency=PERIODIC, interval_days=3)` sets series fields and regenerates.
- `test_update_periodic_to_daily_normalizes_interval` — `update(frequency=DAILY)` sets `interval_days=1`.
- `test_update_interval_only_rejects_daily_series_with_new_interval` — daily series + `interval_days=3` raises `BadRequestError`.
- `test_update_interval_only_allows_daily_no_op` — daily series + `interval_days=1` succeeds without changing the schedule.
- `test_update_interval_only_keeps_periodic_frequency` — periodic series + `interval_days=5` succeeds.
- `test_generate_series_uses_stored_interval` — periodic series over several generation runs never creates off-phase slots.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_task_management_series.py -q
```

Expected: FAIL because `create`/`update` do not accept frequency/interval and `_generate_series` still uses daily generation.

- [ ] **Step 3: Implement series create/update and generation**

In `SeriesService`:
- add a private resolver that normalizes the requested pair:
  - create/conversion: daily -> `1`; periodic -> require `>= 2`;
  - update: if one field is omitted, use the existing series value; daily -> `1`; periodic -> require `>= 2`;
  - interval-only updates on an existing periodic series require `>= 2`; on an existing daily series only `1` is accepted and is a no-op.
- `create` stores `frequency.value` and normalized `interval_days`.
- `update` sets `series.frequency` / `series.interval_days`; include either change in `schedule_changed`.
- on `schedule_changed`, keep the existing soft-delete/regenerate flow and reset `generated_through = None`.
- `_generate_series` calls `series_due_times(..., interval_days=series.interval_days)`.
- update docstrings with new args, `BadRequestError` cases, and periodic semantics.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_series.py tests/test_task_management_schedule.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/task_management/series.py tests/test_task_management_series.py
git commit -m "feat: create and update periodic task series"
```

---

### Task 5: Convert one-off tasks into series and detach series

**Files:**
- Modify: `src/backend/task_management/series.py`
- Test: `tests/test_task_management_series.py`

**Interfaces:**
- Consumes: Task 4 `SeriesService.create/update`, Task 3 repository methods, `reminder_input_from_row`, `build_series_reminder`.
- Produces:
  - `SeriesService.create_from_task(user_id: int, task: Task, *, frequency: SeriesFrequency, interval_days: int | None = None) -> TaskSeries`.
  - `SeriesService.detach_task(user_id: int, task: Task, series: TaskSeries) -> None`.

- [ ] **Step 1: Write the failing conversion/detach tests**

Add to `tests/test_task_management_series.py`:

```python
async def test_create_from_task_snapshots_and_does_not_duplicate_due_slot() -> None:
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Moscow"
    project = repository.insert_project(make_project())
    label = repository.insert_label(make_label())
    due_at = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    task = repository.insert_task(
        make_task(
            due_at=due_at,
            project_id=project.id,
            title="Snapshot",
            description="Description",
            priority=Priority.URGENT_IMPORTANT.value,
            reminders_enabled=True,
        )
    )
    repository.task_labels.add((task.id, label.id))
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=due_at,
        )
    )
    service = make_service(repository)

    series = await service.create_from_task(
        USER_ID,
        task,
        frequency=SeriesFrequency.PERIODIC,
        interval_days=3,
    )

    assert task.series_id == series.id
    assert series.starts_on == date(2026, 11, 7)
    assert series.time_of_day == time(9, 0)
    assert series.timezone == "Europe/Moscow"
    assert series.interval_days == 3
    assert series.reminders_enabled is True
    assert sum(1 for item in repository.tasks.values() if item.due_at == due_at) == 1
    templates = await repository.list_series_reminders(USER_ID, series.id)
    assert templates[0].interval_days == 3
    instance_reminders = await repository.list_task_reminders(USER_ID, task.id)
    assert instance_reminders[0].interval_days == 1


async def test_create_from_task_rejects_archived_project() -> None:
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() + timedelta(days=1),
            project_id=project.id,
        )
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create_from_task(
            USER_ID,
            task,
            frequency=SeriesFrequency.DAILY,
        )
```

Add:

```python
async def test_detach_task_hard_deletes_future_siblings_except_current() -> None:
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    past = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() - timedelta(days=1))
    )
    current = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    earlier_future = repository.insert_task(
        make_task(task_id=3, series_id=series.id, due_at=fixed_now() + timedelta(hours=1))
    )
    later_future = repository.insert_task(
        make_task(task_id=4, series_id=series.id, due_at=fixed_now() + timedelta(days=2))
    )
    completed = repository.insert_task(
        make_task(
            task_id=5,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=3),
            completed_at=fixed_now(),
        )
    )
    overdue = repository.insert_task(
        make_task(task_id=6, series_id=series.id, due_at=fixed_now() - timedelta(hours=1))
    )
    service = make_service(repository)

    await service.detach_task(USER_ID, current, series)

    assert series.deleted_at == fixed_now()
    assert current.series_id is None
    assert {past.id, current.id, completed.id, overdue.id} <= set(repository.tasks)
    assert earlier_future.id not in repository.tasks
    assert later_future.id not in repository.tasks
```

Also add:

- `test_create_from_task_regular_preserves_reminder_interval` — converting to
  daily keeps the current scheduled reminder interval on the template.
- `test_detach_task_keeps_current_completed_task`;
- `test_detach_task_clears_deleted_task_related_rows` (task labels/reminders/
  deliveries for removed tasks).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_task_management_series.py -q
```

Expected: FAIL with missing `create_from_task` / `detach_task`.

- [ ] **Step 3: Implement create_from_task and detach_task**

`create_from_task`:
- assume the caller has excluded an active series; a `series_id` pointing to a
  soft-deleted series is allowed and will be overwritten;
- resolve/normalize frequency and interval;
- resolve author timezone (`repository.get_user_timezone` or `UTC`) with the existing `_timezone` validation;
- derive `starts_on`, `time_of_day` from `task.due_at.astimezone(zone)`;
- validate project and current label ids through the existing helpers;
- convert current task reminders with `reminder_input_from_row`; for
  `SCHEDULED`, replace `interval_days` with the series interval only when the
  new series is periodic; for regular keep the reminder's interval;
- create `TaskSeries` with snapshot fields including `reminders_enabled`,
  `ends_on=None`, `generated_through=None`;
- add series, labels, template reminders;
- set `task.series_id = series.id`;
- call `_generate_series(series, now)`;
- leave the current task's instance reminders untouched;
- flush and return the series.

`detach_task`:
- call `get_series_tasks_for_update(series.id, user_id)`;
- select `task.id != current.id and completed_at is None and due_at > now`;
- call `hard_delete_tasks(tuple(ids), user_id)`;
- call `cancel_series_reminders(user_id, series.id)`;
- call `soft_delete_series(series, now, uuid4())`;
- set `task.series_id = None`;
- flush.

Update docstrings with transaction/lock/history notes.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_series.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/task_management/series.py tests/test_task_management_series.py
git commit -m "feat: convert tasks to series and detach series"
```

---

### Task 6: Task update toggle branches and overdue shift

**Files:**
- Modify: `src/backend/task_management/tasks.py`
- Test: `tests/test_task_management_tasks.py`

**Interfaces:**
- Consumes: `TaskRecurrence`, `next_occurrence_after_date`, `SeriesService.create_from_task`, `SeriesService.detach_task`, `SeriesService.update`.
- Produces:
  - `TaskFilters.recurrence: Literal["all", "regular", "periodic", "one_off"]`.
  - `TaskService.update(..., recurrence: TaskRecurrence | None = None, interval_days: int | None = None)`.
  - `TaskService._apply_task_update(...) -> None`.
  - `TaskService._shift_overdue_task(..., anchor: date, interval_days: int, timezone: ZoneInfo) -> None`.

- [ ] **Step 1: Write the failing toggle tests**

Add to `tests/test_task_management_tasks.py`:

```python
async def test_enable_regular_on_future_one_off_task_creates_series_without_moving_due() -> None:
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Moscow"
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() + timedelta(days=1),
            title="Old",
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        title="New",
        recurrence=TaskRecurrence.REGULAR,
    )

    assert detail.task.title == "New"
    assert detail.task.series_id is not None
    assert detail.recurrence is TaskRecurrence.REGULAR
    assert detail.task.due_at == fixed_now() + timedelta(days=1)
    series = repository.series[detail.task.series_id]
    assert series.frequency == "daily"
    assert series.interval_days == 1


async def test_enable_periodic_on_overdue_one_off_task_shifts_to_next_phase_slot() -> None:
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Moscow"
    task = repository.insert_task(
        make_task(due_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        recurrence=TaskRecurrence.PERIODIC,
        interval_days=3,
    )

    assert detail.task.due_at == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    series = repository.series[detail.task.series_id]
    assert series.starts_on == date(2026, 11, 7)
    assert series.interval_days == 3
    assert detail.task.due_at in {item.due_at for item in repository.tasks.values()}
    assert sum(1 for item in repository.tasks.values() if item.due_at == detail.task.due_at) == 1


async def test_enable_regular_on_completed_task_keeps_completion_and_series_starts_on_due_date() -> None:
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() - timedelta(days=2),
            completed_at=fixed_now() - timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        recurrence=TaskRecurrence.REGULAR,
    )

    assert detail.recurrence is TaskRecurrence.REGULAR
    assert detail.task.completed_at == fixed_now() - timedelta(days=1)
    assert detail.task.due_at == fixed_now() - timedelta(days=2)
```

Add:
- `test_disable_regular_on_future_series_task_detaches_only_current_instance` — other future incomplete siblings physically removed; current remains future; series soft-deleted; `series_id is None`; `recurrence is ONE_OFF`.
- `test_disable_regular_on_overdue_series_task_shifts_current_and_keeps_history` — current incomplete overdue shifts by series anchor; past/completed/overdue siblings remain; series soft-deleted.
- `test_disable_overdue_series_task_can_reuse_a_removed_future_slot` — a
  future sibling already occupies the shift target; detach removes it first and
  the current task moves there without a unique-slot conflict.
- `test_disable_regular_on_completed_series_task_keeps_due` — completed task unchanged, `series_id is None`.
- `test_update_periodic_series_interval_without_recurrence` — periodic series + `interval_days=5` updates and regenerates.
- `test_interval_only_on_one_off_or_daily_raises_bad_request` — both cases raise `BadRequestError`.
- `test_periodic_requires_interval_on_conversion` — `recurrence=PERIODIC`, no interval raises `BadRequestError`.
- `test_enable_regular_on_task_with_archived_project_raises_bad_request` and deleted project raises `NotFoundError`.
- `test_update_task_of_soft_deleted_series_treats_it_as_one_off` — no `NotFoundError`; title changes on the same task; series remains deleted.
- `test_disable_regular_on_task_of_soft_deleted_series_clears_series_id` —
  PATCH `recurrence=one_off` clears the stale `series_id`; the old series stays
  soft-deleted and the response recurrence is `one_off`.
- `test_reenable_regular_on_task_of_soft_deleted_series_creates_new_series` —
  PATCH `recurrence=regular` on a task whose `series_id` points to a soft-deleted
  series creates a new active series, overwrites `task.series_id`, and leaves
  the old soft-deleted series/history unchanged.
- `test_toggle_applies_other_fields_before_conversion` — title/description/priority/project/labels applied to current task and reflected in series snapshot.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_task_management_tasks.py -q
```

Expected: FAIL because `update` does not accept `recurrence`/`interval_days`.

- [ ] **Step 3: Extract `_apply_task_update` and implement branches**

- Move the current one-off update body into `_apply_task_update`; keep its validation, label replacement, reminder rebuild, and toggle handling.
- Add `recurrence`/`interval_days` to `update`.
- If `recurrence is ONE_OFF` and `interval_days is not None`: raise `BadRequestError`.
- Compute `target_recurrence = recurrence`.
- If `series is None` (one-off task or a task whose `series_id` points to a
  soft-deleted series):
  - if `recurrence in (None, ONE_OFF)` and `interval_days is None`: for an
    explicit `ONE_OFF`, first clear a stale `task.series_id` pointing to a
    soft-deleted series, then `_apply_task_update`;
  - if `recurrence in (REGULAR, PERIODIC)`: if `task.series_id` points to a
    soft-deleted series, clear it before changing `due_at` (avoids the old
    `(series_id, due_at)` unique conflict); then `_apply_task_update`; regular
    accepts only `interval_days` `None`/`1` and normalizes to `1`; periodic
    requires `interval_days >= 2`; shift if incomplete and `due_at <= now`
    with author timezone anchor = final due local date; call `create_from_task`;
  - `interval_days` without recurrence on a one-off -> `BadRequestError`.
- If `series is not None`:
  - `recurrence == ONE_OFF`: reject `interval_days`; capture
    `series.starts_on`, `series.interval_days`, `ZoneInfo(series.timezone)`;
    call `detach_task` first (removes future siblings and clears
    `task.series_id`, avoiding a `(series_id, due_at)` unique conflict); then
    `_apply_task_update` on the now-detached task; then shift if overdue using
    the captured series phase. The request transaction keeps this atomic.
  - `recurrence in (REGULAR, PERIODIC)` or `recurrence is None` with `interval_days`: call `_update_series_task` with frequency/interval. Regular -> daily/1; periodic -> periodic with supplied or existing valid interval; interval-only requires existing periodic.
  - no recurrence and no interval: existing `_update_series_task` behavior.
- Implement `_shift_overdue_task`:
  - no-op when completed or `due_at > now`;
  - derive `time_of_day` from current due in the supplied zone;
  - call `next_occurrence_after_date`;
  - set `task.due_at`;
  - call `_rebuild_task_reminders(user_id, task, reminders=None)`.
- Update `_update_series_task` to pass `frequency`/`interval_days` to `SeriesService.update`.
- Update all docstrings.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_tasks.py tests/test_task_management_series.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/task_management/tasks.py tests/test_task_management_tasks.py
git commit -m "feat: toggle task recurrence modes"
```

---

### Task 7: DELETE matrix, soft-deleted series handling, and task recurrence detail

**Files:**
- Modify: `src/backend/task_management/tasks.py`
- Test: `tests/test_task_management_tasks.py`

**Interfaces:**
- Consumes: Task 3 `get_series(include_deleted=True)` and `get_series_by_ids`, Task 5 `detach_task`.
- Produces:
  - `TaskDetail.recurrence: TaskRecurrence`.
  - `TaskService.delete` branching by task state and series presence.
  - `_lock_update_target` treating soft-deleted series as one-off.
  - `_detail` / `_details` compute recurrence with deleted series.

- [ ] **Step 1: Write the failing delete/detail tests**

Add to `tests/test_task_management_tasks.py`:

```python
async def test_delete_completed_series_task_deletes_only_that_task() -> None:
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    completed = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    future = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=2))
    )
    service = make_service(repository)

    await service.delete(USER_ID, completed.id)

    assert completed.deleted_at == fixed_now()
    assert series.deleted_at is None
    assert future.deleted_at is None


async def test_delete_overdue_series_task_deletes_only_that_task() -> None:
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    overdue = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() - timedelta(hours=1))
    )
    future = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, overdue.id)

    assert overdue.deleted_at == fixed_now()
    assert series.deleted_at is None
    assert future.deleted_at is None


async def test_delete_task_of_soft_deleted_series_deletes_only_task() -> None:
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, deleted_at=fixed_now())
    )
    task = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, task.id)

    assert task.deleted_at == fixed_now()
    assert series.deleted_at == fixed_now()


@pytest.mark.parametrize(
    ("frequency", "interval_days", "expected"),
    [
        (None, None, TaskRecurrence.ONE_OFF),
        ("daily", 1, TaskRecurrence.REGULAR),
        ("periodic", 3, TaskRecurrence.PERIODIC),
    ],
)
async def test_task_detail_recurrence_uses_series_frequency(
    frequency: str | None,
    interval_days: int,
    expected: TaskRecurrence,
) -> None:
    repository = FakeTaskManagementRepository()
    series_id = None
    if frequency is not None:
        series = repository.insert_series(
            make_series(
                series_id=1,
                frequency=frequency,
                interval_days=interval_days,
                time_of_day=time(9, 0),
                starts_on=date(2026, 11, 1),
            )
        )
        series_id = series.id
    task = repository.insert_task(
        make_task(series_id=series_id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    detail = await service.get(USER_ID, task.id)

    assert detail.recurrence is expected
```

Add:
- `test_task_detail_recurrence_survives_soft_deleted_series`.
- `test_list_tasks_returns_recurrence_for_mixed_page` — one one-off, one daily task, one periodic task, one task of a soft-deleted periodic series; assert classifications.
- `test_delete_active_future_series_task_still_deletes_series_and_all_future_instances` — includes an earlier future sibling; both soft-deleted.
- `test_lock_update_target_treats_soft_deleted_series_as_one_off` — update by task id does not raise, changes task only.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_task_management_tasks.py -q
```

Expected: FAIL because `TaskDetail` has no `recurrence`, delete always delegates to `SeriesService.delete`, and `_lock_update_target` raises for deleted series.

- [ ] **Step 3: Implement delete/detail/lock changes**

- Add `recurrence: TaskRecurrence` to `TaskDetail`.
- `_lock_update_target`: call `get_series(..., for_update=True, include_deleted=True)`; if the series is soft-deleted, return `(task, None)` after locking both rows.
- `_detail`: if `task.series_id is not None`, load `get_series(..., include_deleted=True)`; classify daily -> `REGULAR`, periodic -> `PERIODIC`; missing -> `ONE_OFF`.
- `_details`: fetch distinct series ids and call `get_series_by_ids(..., include_deleted=True)`; classify each task; skip the series batch query entirely when no task has a `series_id`; an empty page issues no batch queries.
- `delete`:
  - if `series is None`: cancel task reminders, soft-delete task;
  - if `series is not None and task.completed_at is None and task.due_at > now`: `SeriesService.delete`;
  - else: cancel task reminders, soft-delete only the task.
- Update docstrings for DELETE, lock order, and recurrence.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_task_management_tasks.py tests/test_task_management_series.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/task_management/tasks.py tests/test_task_management_tasks.py
git commit -m "feat: apply recurrence-aware delete semantics"
```

---

### Task 8: API schemas and routes

**Files:**
- Modify: `src/backend/api/task_schemas.py`
- Modify: `src/backend/api/routes/tasks.py`
- Modify: `src/backend/api/routes/task_series.py`
- Test: `tests/test_tasks_api.py`
- Test: `tests/test_task_series_api.py`

**Interfaces:**
- Consumes: `TaskRecurrence`, `TaskDetail.recurrence`, new service signatures.
- Produces:
  - `TaskUpdate.recurrence`, `TaskUpdate.interval_days` with cross-field validation.
  - `TaskResponse.recurrence`.
  - `TaskFiltersQuery.recurrence` accepting `periodic`.
  - `SeriesCreate/SeriesUpdate.frequency`, `interval_days`.
  - `SeriesResponse.frequency: daily|periodic`, `SeriesResponse.interval_days`.

- [ ] **Step 1: Write the failing API tests**

In `tests/test_tasks_api.py`:
- update `_task_detail` with `recurrence: TaskRecurrence = TaskRecurrence.ONE_OFF`;
- add `assert body["recurrence"] == "one_off"` to the create/read test;
- add a list filter assertion for `("recurrence", "periodic")`;
- add response tests for regular/periodic details;
- add PATCH validation parametrization:

```python
@pytest.mark.parametrize(
    "payload",
    [
        {"recurrence": "one_off", "interval_days": None},
        {"recurrence": "one_off", "interval_days": 3},
        {"recurrence": "regular", "interval_days": 3},
        {"recurrence": "periodic"},
        {"recurrence": "periodic", "interval_days": 1},
        {"interval_days": 1},
    ],
)
async def test_task_update_rejects_invalid_recurrence_interval(
    task_client: AsyncClient,
    payload: dict[str, object],
) -> None:
    response = await task_client.patch("/api/v1/tasks/1", json=payload)
    assert response.status_code == 422
```

Add accepted cases:
- `{"recurrence": "regular"}`;
- `{"recurrence": "regular", "interval_days": 1}`;
- `{"recurrence": "regular", "interval_days": None}`;
- `{"recurrence": "periodic", "interval_days": 3}`;
- `{"interval_days": 5}`.
Assert the fake service receives the exact values.
- Add a route-level DELETE test that the route returns `204` and delegates to `service.delete`; service tests own the full delete matrix.
- Update `_filters_fingerprint` expectations only if the cursor tests build the mapping manually (they do; keep `recurrence` key).

In `tests/test_task_series_api.py`:
- add `interval_days=1` to `_series_detail`;
- assert create response includes `"interval_days": 1`;
- add `test_create_periodic_series_returns_frequency_and_interval` with payload
  `{"frequency": "periodic", "interval_days": 3}` and fake service recording;
- add `test_update_series_passes_frequency_and_interval`;
- add invalid cases: `daily + 3`, `periodic` without interval, `periodic + 1`;
- add accepted case `{"interval_days": 5}` for a periodic series at the service boundary (API schema allows it; service validates existence).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
poetry run pytest tests/test_tasks_api.py tests/test_task_series_api.py -q
```

Expected: FAIL because schemas and routes do not expose the new fields.

- [ ] **Step 3: Implement schemas and routes**

`TaskUpdate`:
- add `recurrence: TaskRecurrence | None = None`;
- add `interval_days: int | None = Field(default=None, ge=1, le=32_767)`;
- add `model_validator(mode="after")` using `self.model_fields_set`:
  - one_off rejects any supplied `interval_days`, including null;
  - regular allows only null/1;
  - periodic requires supplied `interval_days >= 2`;
  - recursion absent + interval supplied requires `>= 2` (service later verifies the target series is periodic).

`TaskResponse`: add `recurrence: TaskRecurrence`.

`TaskFiltersQuery`: `Literal["all", "regular", "periodic", "one_off"]`.

`SeriesCreate`:
- `frequency: SeriesFrequency = SeriesFrequency.DAILY`;
- `interval_days: int | None = Field(default=None, ge=1, le=32_767)`;
- validator: daily allows null/1; periodic requires >= 2.

`SeriesUpdate`:
- add `frequency: SeriesFrequency | None = None`, `interval_days: int | None`;
- validator: if both supplied, daily allows null/1; periodic supplied interval must be >= 2; interval-only is left to the service.

`SeriesResponse`: `frequency: SeriesFrequency`, `interval_days: int`.

Routes:
- `_task_response` passes `recurrence=detail.recurrence`;
- `_task_changes` already forwards unset fields; ensure recurrence/interval values reach the service;
- `_series_response` passes frequency and interval_days;
- `create_series` passes frequency/interval_days;
- update docstrings/OpenAPI notes for toggle, periodic, DELETE.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
poetry run pytest tests/test_tasks_api.py tests/test_task_series_api.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backend/api/task_schemas.py src/backend/api/routes/tasks.py src/backend/api/routes/task_series.py tests/test_tasks_api.py tests/test_task_series_api.py
git commit -m "feat: expose recurrence and periodic series API"
```

---

### Task 9: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-11-03-task-management-database-design.md`
- Verify: `docs/superpowers/specs/2026-11-04-task-recurrence-toggle-design.md`
- Verify: `docs/superpowers/plans/2026-11-04-task-recurrence-toggle.md`

- [ ] **Step 1: Update README**

- Task list filters: `recurrence` now includes `periodic`.
- Task response example/description: add `recurrence`.
- `PATCH /api/v1/tasks/{id}`: document `recurrence` and `interval_days`, including
  the validation matrix and “fields first, then conversion”.
- DELETE section: replace the old blanket series-delete rule with the four-case
  matrix.
- Task-series section: `frequency` is `daily|periodic`; document
  `interval_days`, periodic generation phase, and backward compatibility.
- Scheduler section: “ежедневных серий” -> “daily/periodic series”.

- [ ] **Step 2: Update the old database design spec pointwise**

In `docs/superpowers/specs/2026-11-03-task-management-database-design.md`:
- goals/non-goals: allow periodic `N >= 2`, remove the “only daily” non-goal;
- decisions: replace “regularity only daily” with daily + periodic; update
  single-instance non-goal wording for detach;
- `task_series` schema: add `interval_days` and new checks;
- enum values: `frequency = daily|periodic`; add `TaskRecurrence` API values;
- lifecycle 9.3: toggle one_off/regular/periodic, due shift, physical detach
  deletion;
- lifecycle 9.5: four-case DELETE matrix;
- lifecycle 9.7: phase-aware periodic generation;
- DST section: phase arithmetic for periodic;
- API impact: `recurrence`, `interval_days`.
- Add a note that the detailed toggle design lives in
  `2026-11-04-task-recurrence-toggle-design.md`.

- [ ] **Step 3: Run the complete verification**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
```

Expected: all commands exit `0`; graphify reports an updated graph.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-11-03-task-management-database-design.md docs/superpowers/specs/2026-11-04-task-recurrence-toggle-design.md docs/superpowers/plans/2026-11-04-task-recurrence-toggle.md
git commit -m "docs: describe task recurrence toggles"
```
