# Task Management Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement projects, labels, one-off tasks, daily recurring tasks, reminders, delivery journal, and the global color palette from the task-management database spec.

**Architecture:** Add a `backend.task_management` domain package with pure scheduling functions, service classes, a repository port, and a SQLAlchemy repository. Add ORM models and one hand-written Alembic migration, then expose the domain through FastAPI routes and Taskiq jobs. Reminder delivery uses the existing NATS broker plus a Telegram sender port.

**Tech Stack:** Python 3.14, SQLAlchemy 2 (async), Alembic, PostgreSQL, Pydantic 2, FastAPI, Taskiq, zoneinfo.

**Spec:** `docs/superpowers/specs/2026-11-03-task-management-database-design.md`

## Global Constraints

- No new runtime or dev Python dependencies. Use the standard library, including `zoneinfo` and `urllib.request`, for time and Telegram HTTP.
- All absolute moments are `TIMESTAMPTZ` in UTC. Local dates/times are interpreted in `task_series.timezone` or `users.timezone`.
- Daily recurrence only: `frequency = 'daily'`. One-off tasks have `series_id IS NULL`.
- Materialization horizon is 90 days. Past occurrences are never backfilled.
- `due_at` is mandatory. Default priority is `neither`.
- Soft delete applies to `projects`, `labels`, `task_series`, and `tasks`; archive is `archived_at` on projects.
- All read queries filter `deleted_at IS NULL`; archived projects are excluded from active lists.
- A task or series has at most one reminder of each kind (`before_due`, `scheduled`).
- Delivery idempotency is enforced by `UNIQUE (reminder_id, scheduled_for)`.
- Palette is global, seeded with 15 colors, and only active colors are selectable. `grape` is first.
- Deleting or deactivating the last active palette color returns `409 conflict`.
- Enumerations are `VARCHAR` plus `CHECK`, never PostgreSQL ENUM.
- Primary keys are `BIGINT` with `Identity()`.
- Composite foreign keys include `user_id` to prevent cross-user links.
- Every new function, method, and class has an English docstring with `Purpose`, and `Args`/`Returns`/`Raises` where applicable.
- No comments in application code.
- Verification after every task: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`, `graphify update .`.
- Example tasks `ping` and `heartbeat` and the `POST /api/v1/tasks/ping` endpoint must be deleted. `compose.yml` must load the real task modules instead of `backend.tasks.example`.

## Review Focus

- DST boundaries: a nonexistent spring-forward local time must move forward to the first valid instant, and an ambiguous fall-back time must use `fold=0`.
- No backfill: creating a series after today's slot must not create yesterday's or today's missed occurrences.
- Reminder idempotency: dispatching the same due reminder twice must create at most one delivery per `(reminder_id, scheduled_for)` and never resend a `sent` delivery.
- Tenant isolation: a project, label, task, series, or reminder from another user must behave as not found; composite FKs are the database backstop.
- Soft delete/archive: deleted or archived projects and tasks must disappear from active lists, stop generation, and cancel pending deliveries.

---

### Task 1: Scheduling and timezone primitives

**Files:**
- Create: `src/backend/task_management/__init__.py`
- Create: `src/backend/task_management/enums.py`
- Create: `src/backend/task_management/schedule.py`
- Test: `tests/test_task_management_schedule.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Priority` (`StrEnum`): `URGENT_IMPORTANT = "urgent_important"`, `IMPORTANT_NOT_URGENT = "important_not_urgent"`, `URGENT_NOT_IMPORTANT = "urgent_not_important"`, `NEITHER = "neither"`.
  - `SeriesFrequency` (`StrEnum`): `DAILY = "daily"`.
  - `ReminderKind` (`StrEnum`): `BEFORE_DUE = "before_due"`, `SCHEDULED = "scheduled"`.
  - `DeliveryStatus` (`StrEnum`): `PENDING`, `SENDING`, `SENT`, `FAILED`, `CANCELLED`.
  - `HORIZON_DAYS: int = 90`.
  - `localize_wall_time(local_date: date, time_of_day: time, timezone: ZoneInfo) -> datetime` returning an aware UTC datetime.
  - `daily_due_times(*, starts_on: date, ends_on: date | None, time_of_day: time, timezone: ZoneInfo, now: datetime, generated_through: datetime | None = None, horizon_days: int = HORIZON_DAYS) -> list[datetime]` returning UTC datetimes in ascending order.
  - `next_daily_time(*, current: datetime, time_of_day: time, timezone: ZoneInfo) -> datetime`.
  - `before_due_time(*, due_at: datetime, offset_minutes: int) -> datetime`.
  - `first_scheduled_time(*, due_at: datetime, time_of_day: time, timezone: ZoneInfo, interval_days: int, now: datetime) -> datetime`.
  - `next_scheduled_time(*, current: datetime, time_of_day: time, timezone: ZoneInfo, interval_days: int) -> datetime`.

- [ ] **Step 1: Write the failing scheduling tests**

```python
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from backend.task_management.schedule import (
    before_due_time,
    daily_due_times,
    first_scheduled_time,
    next_scheduled_time,
)

MOSCOW = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def test_daily_due_times_skips_past_and_starts_tomorrow() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result[0] == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)


def test_daily_due_times_uses_today_when_slot_is_ahead() -> None:
    now = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result[0] == datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def test_daily_due_times_includes_future_start_even_beyond_horizon() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2027, 6, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == [datetime(2027, 6, 1, 6, 0, tzinfo=UTC)]


def test_daily_due_times_stops_at_ends_on_inclusive() -> None:
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 5),
        ends_on=date(2026, 11, 7),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == [
        datetime(2026, 11, 5, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 6, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
    ]


def test_daily_due_times_spring_forward_uses_first_valid_instant() -> None:
    now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 3, 29),
        ends_on=date(2026, 3, 29),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
    )
    assert result == [datetime(2026, 3, 29, 1, 0, tzinfo=UTC)]


def test_daily_due_times_fall_back_uses_first_occurrence() -> None:
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 10, 25),
        ends_on=date(2026, 10, 25),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
    )
    assert result == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]


def test_before_due_time_subtracts_offset() -> None:
    due_at = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert before_due_time(due_at=due_at, offset_minutes=30) == datetime(
        2026, 11, 4, 11, 30, tzinfo=UTC
    )


def test_first_scheduled_time_uses_next_local_slot_after_due() -> None:
    due_at = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    now = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    result = first_scheduled_time(
        due_at=due_at,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=1,
        now=now,
    )
    assert result == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)


def test_first_scheduled_time_respects_interval() -> None:
    due_at = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    result = first_scheduled_time(
        due_at=due_at,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=3,
        now=now,
    )
    assert result == datetime(2026, 11, 11, 6, 0, tzinfo=UTC)


def test_next_scheduled_time_advances_by_interval() -> None:
    current = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    assert next_scheduled_time(
        current=current,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=2,
    ) == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_task_management_schedule.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.task_management'`.

- [ ] **Step 3: Implement the enums and scheduling functions**

Implementation rules:
- `localize_wall_time` builds a naive `datetime.combine`, attaches the zone with `fold=0`, converts to UTC, and converts back. When the wall clock differs (a spring-forward gap), return the first valid instant after the gap: clamp the wall clock to the first minute of the gap's end (for example 02:30 in Europe/Berlin on 2026-03-29 becomes 03:00 local, 01:00 UTC). Raise `ValueError` when `time_of_day` is timezone-aware.
- `daily_due_times` computes the first candidate from `starts_on`. If `generated_through` is set, start on the next local day after it. If the candidate is `<= now`, advance to the next local day whose slot is `> now`. Emit daily candidates while the candidate is `<= min(now + 90 days, end of ends_on)`; if the first valid candidate is beyond the horizon, emit exactly that one candidate. Never emit a candidate `<= now`.
- `first_scheduled_time` starts from the first local `time_of_day` on or after `due_at`, then advances by `interval_days` local days until the candidate is `> now`.
- `next_scheduled_time` converts `current` to local time, adds `interval_days` days, and re-localizes `time_of_day`.
- `daily_due_times` must short-circuit when `ends_on < starts_on` or `ends_on` is before the current local date.
- Add tests for `interval_days` values `0` and `-1` raising `ValueError`, and for `generated_through` already beyond the horizon.
- Use `datetime.now(UTC)` only as a default in services, never inside these pure functions.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `poetry run pytest tests/test_task_management_schedule.py -q`
Expected: all tests pass.

- [ ] **Step 5: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/task_management tests/test_task_management_schedule.py
git commit -m "feat: add task scheduling primitives"
```

---

### Task 2: ORM models and user timezone

**Files:**
- Create: `src/backend/models/color_palette.py`
- Create: `src/backend/models/project.py`
- Create: `src/backend/models/label.py`
- Create: `src/backend/models/task_series.py`
- Create: `src/backend/models/task.py`
- Create: `src/backend/models/task_label.py`
- Create: `src/backend/models/series_label.py`
- Create: `src/backend/models/task_reminder.py`
- Create: `src/backend/models/reminder_delivery.py`
- Modify: `src/backend/models/user.py`
- Modify: `src/backend/models/__init__.py`
- Test: `tests/test_task_management_models.py`

**Interfaces:**
- Consumes: enum values from `backend.task_management.enums`.
- Produces ORM classes: `ColorPalette`, `Project`, `Label`, `TaskSeries`, `Task`, `TaskLabel`, `SeriesLabel`, `TaskReminder`, `ReminderDelivery`; `User.timezone`.

- [ ] **Step 1: Write the failing metadata tests**

```python
from sqlalchemy import BigInteger, CHAR, DateTime, Identity, String

import backend.models  # noqa: F401
from backend.db.base import Base


def test_task_management_tables_are_registered() -> None:
    assert {
        "color_palette",
        "projects",
        "labels",
        "task_series",
        "tasks",
        "task_labels",
        "series_labels",
        "task_reminders",
        "reminder_deliveries",
    } <= set(Base.metadata.tables)


def test_user_timezone_defaults_to_utc() -> None:
    table = Base.metadata.tables["users"]
    assert isinstance(table.c.timezone.type, String)
    assert table.c.timezone.type.length == 64
    assert table.c.timezone.nullable is False
    assert table.c.timezone.server_default is not None


def test_task_management_primary_keys_use_identity() -> None:
    for table_name in (
        "color_palette",
        "projects",
        "labels",
        "task_series",
        "tasks",
        "task_reminders",
        "reminder_deliveries",
    ):
        column = Base.metadata.tables[table_name].c.id
        assert isinstance(column.type, BigInteger)
        assert isinstance(column.identity, Identity)


def test_color_palette_columns_and_indexes() -> None:
    table = Base.metadata.tables["color_palette"]
    assert isinstance(table.c.hex.type, CHAR)
    assert table.c.hex.type.length == 7
    assert table.c.slug.unique is True
    index_names = {index.name for index in table.indexes}
    assert "ix_color_palette_is_active_sort_order_id" in index_names


def test_tasks_have_timestamptz_due_and_completion() -> None:
    table = Base.metadata.tables["tasks"]
    assert isinstance(table.c.due_at.type, DateTime)
    assert table.c.due_at.type.timezone is True
    assert isinstance(table.c.completed_at.type, DateTime)
    assert table.c.completed_at.type.timezone is True


def test_composite_foreign_keys_include_user_id() -> None:
    tasks = Base.metadata.tables["tasks"]
    targets = {
        tuple(column.name for column in fk.columns): fk.target_fullname
        for fk in tasks.foreign_keys
    }
    assert targets[("project_id", "user_id")] == "projects.id"
    assert targets[("series_id", "user_id")] == "task_series.id"


def test_partial_unique_indexes_are_declared() -> None:
    tasks = Base.metadata.tables["tasks"]
    series_slot = next(
        index for index in tasks.indexes if index.name == "uq_tasks_series_id_due_at"
    )
    assert series_slot.unique is True
    assert series_slot.dialect_options["postgresql"]["where"] is not None

    reminders = Base.metadata.tables["task_reminders"]
    kind_indexes = {
        index.name
        for index in reminders.indexes
        if index.name in {"uq_task_reminders_task_id_kind", "uq_task_reminders_series_id_kind"}
    }
    assert kind_indexes == {
        "uq_task_reminders_task_id_kind",
        "uq_task_reminders_series_id_kind",
    }


def test_delivery_has_idempotency_key() -> None:
    table = Base.metadata.tables["reminder_deliveries"]
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("reminder_id", "scheduled_for") in unique_columns
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_task_management_models.py -q`
Expected: FAIL because the tables and `users.timezone` do not exist.

- [ ] **Step 3: Implement the models**

Required table definitions (column names, types, nullability, defaults, checks, indexes, and FKs are binding):

- `color_palette`: `id`, `slug VARCHAR(32) UNIQUE`, `name VARCHAR(64)`, `hex CHAR(7)`, `sort_order SMALLINT`, `is_active BOOLEAN DEFAULT true`, `created_at`, `updated_at`. Checks: `slug ~ '^[a-z0-9_-]+$'`, `hex ~ '^#[0-9A-Fa-f]{6}$'`. Index `(is_active, sort_order, id)`.
- `projects`: `id`, `user_id FK users.id ON DELETE CASCADE`, `name VARCHAR(120)`, `description TEXT NULL`, `color_id FK color_palette.id ON DELETE RESTRICT`, `icon VARCHAR(32) NULL`, `archived_at`, `deleted_at`, `deletion_batch_id UUID NULL`, timestamps. `UNIQUE (id, user_id)`; partial unique index `(user_id, lower(name)) WHERE deleted_at IS NULL AND archived_at IS NULL`; index `(user_id, archived_at) WHERE deleted_at IS NULL`; index `(color_id)`.
- `labels`: `id`, `user_id`, `name VARCHAR(60)`, `color_id`, `deleted_at`, timestamps. `UNIQUE (id, user_id)`; partial unique `(user_id, lower(name)) WHERE deleted_at IS NULL`; index `(user_id) WHERE deleted_at IS NULL`; index `(color_id)`.
- `task_series`: `id`, `user_id`, `project_id NULL`, `title VARCHAR(120)`, `description TEXT NULL`, `priority VARCHAR(32) DEFAULT 'neither'`, `frequency VARCHAR(16) DEFAULT 'daily'`, `time_of_day TIME`, `timezone VARCHAR(64)`, `starts_on DATE`, `ends_on DATE NULL`, `generated_through TIMESTAMPTZ NULL`, `reminders_enabled BOOLEAN DEFAULT false`, `deleted_at`, `deletion_batch_id UUID NULL`, timestamps. `UNIQUE (id, user_id)`; checks: `frequency = 'daily'`, `ends_on IS NULL OR ends_on >= starts_on`, priority set, frequency set; FK `(project_id, user_id) -> projects(id, user_id) ON DELETE RESTRICT`; index `(user_id, generated_through) WHERE deleted_at IS NULL`.
- `tasks`: `id`, `user_id`, `series_id NULL`, `project_id NULL`, `title VARCHAR(120)`, `description TEXT NULL`, `priority VARCHAR(32) DEFAULT 'neither'`, `due_at TIMESTAMPTZ`, `completed_at TIMESTAMPTZ NULL`, `reminders_enabled BOOLEAN DEFAULT false`, `deleted_at`, `deletion_batch_id UUID NULL`, timestamps. `UNIQUE (id, user_id)`; check: priority set. Partial unique `(series_id, due_at) WHERE series_id IS NOT NULL AND deleted_at IS NULL`; FKs `(project_id, user_id)` and `(series_id, user_id)` with `ON DELETE RESTRICT`; indexes `(user_id, due_at, id)`, `(user_id, completed_at)`, `(user_id, project_id, due_at)`, `(user_id, priority, due_at)`, `(user_id, series_id)`, `(project_id, completed_at)`, all `WHERE deleted_at IS NULL`.
- `task_labels`: `task_id`, `label_id`, `user_id`, `created_at`; PK `(task_id, label_id)`; FK `(task_id, user_id) -> tasks(id, user_id) ON DELETE CASCADE`; FK `(label_id, user_id) -> labels(id, user_id) ON DELETE CASCADE`; indexes `(label_id, task_id)`, `(user_id, label_id)`.
- `series_labels`: same shape with `series_id`; FK `(series_id, user_id) -> task_series(id, user_id) ON DELETE CASCADE`; indexes `(label_id, series_id)`, `(user_id, label_id)`.
- `task_reminders`: `id`, `user_id`, `task_id NULL`, `series_id NULL`, `kind VARCHAR(16)`, `enabled BOOLEAN DEFAULT true`, `offset_minutes INTEGER NULL`, `interval_days SMALLINT NULL`, `time_of_day TIME NULL`, `timezone VARCHAR(64) NULL`, `next_run_at TIMESTAMPTZ NULL`, timestamps. `UNIQUE (id, user_id)`; check exactly one owner: `(CASE WHEN task_id IS NULL THEN 0 ELSE 1 END) + (CASE WHEN series_id IS NULL THEN 0 ELSE 1 END) = 1`; checks for kind, `offset_minutes >= 0`, `interval_days > 0`; partial unique `(task_id, kind) WHERE task_id IS NOT NULL`, `(series_id, kind) WHERE series_id IS NOT NULL`; FKs `(task_id, user_id)`, `(series_id, user_id)` with `ON DELETE CASCADE`; index `(next_run_at) WHERE enabled AND next_run_at IS NOT NULL`.
- `reminder_deliveries`: columns exactly as the spec; `UNIQUE (reminder_id, scheduled_for)`; check `attempts >= 0` and status set; FK `(reminder_id, user_id) -> task_reminders(id, user_id) ON DELETE CASCADE`; indexes `(status, next_attempt_at)`, `(user_id, scheduled_for)`.
- Add `timezone` to `users` with `server_default=text("'UTC'")`, `default="UTC"`, `nullable=False`.
- Export all new classes in `backend/models/__init__.py`.
- Do not add ORM relationships unless needed to construct a model; services query explicitly.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `poetry run pytest tests/test_task_management_models.py -q`
Expected: all tests pass.

- [ ] **Step 5: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/models tests/test_task_management_models.py
git commit -m "feat: add task management ORM models"
```

---

### Task 3: Migration, palette seed, and SQLAdmin palette view

**Files:**
- Create: `alembic/versions/0003_task_management.py`
- Modify: `src/backend/admin/views.py`
- Modify: `src/backend/admin/setup.py`
- Test: `tests/test_task_management_migration.py`
- Test: `tests/test_admin_views.py`

**Interfaces:**
- Consumes: ORM models from Task 2.
- Produces: revision `0003_task_management`; `ColorPaletteAdmin`.

- [ ] **Step 1: Write the failing migration and admin tests**

```python
import importlib.util
from pathlib import Path

from backend.admin.views import ColorPaletteAdmin
from backend.models.color_palette import ColorPalette


def _load_migration():
    path = Path("alembic/versions/0003_task_management.py")
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_revision_chain() -> None:
    migration = _load_migration()
    assert migration.revision == "0003_task_management"
    assert migration.down_revision == "0002_telegram_auth"


def test_migration_seeds_fifteen_unique_colors() -> None:
    migration = _load_migration()
    assert len(migration.SEED_COLORS) == 15
    assert [row["slug"] for row in migration.SEED_COLORS][0] == "grape"
    assert len({row["slug"] for row in migration.SEED_COLORS}) == 15
    for row in migration.SEED_COLORS:
        assert len(row["hex"]) == 7 and row["hex"].startswith("#")


def test_palette_admin_is_bound_to_model() -> None:
    assert ColorPaletteAdmin.model is ColorPalette
    assert ColorPaletteAdmin.identity == "color-palette"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_task_management_migration.py tests/test_admin_views.py -q`
Expected: FAIL because migration and admin view do not exist.

- [ ] **Step 3: Write the hand-written migration**

Create revision `0003_task_management` descending from `0002_telegram_auth`. Define `SEED_COLORS` at module level as a tuple of 15 dicts with keys `slug`, `name`, `hex`, `sort_order`. Use the exact values from the spec. In `upgrade()`:
1. create `color_palette`;
2. `op.bulk_insert` with `SEED_COLORS`;
3. add `users.timezone`;
4. create `projects`, `labels`, `task_series`, `tasks`, `task_labels`, `series_labels`, `task_reminders`, `reminder_deliveries`;
5. create all partial unique indexes and list indexes with `postgresql_where=sa.text(...)`.

In `downgrade()`, drop indexes and tables in reverse order, drop `users.timezone`, and drop `color_palette` last. Follow the existing migration style for docstrings and revision variables.

- [ ] **Step 4: Implement the SQLAdmin palette view**

Add `ColorPaletteAdmin(ModelView, model=ColorPalette)` to `backend/admin/views.py`:
- `column_list`: id, slug, name, hex, sort_order, is_active.
- `column_searchable_list`: slug, name.
- `column_sortable_list`: id, sort_order, name.
- `column_filters`: `BooleanFilter(ColorPalette.is_active)`, `OperationColumnFilter(ColorPalette.slug)`.
- `column_default_sort = (ColorPalette.sort_order, False)`.
- `form_excluded_columns = [ColorPalette.id]`.
- `on_model_change`: reject changing `slug` after creation and reject deactivating the last active color. Query with `async with self.session_maker() as session:` and `select(func.count()).select_from(ColorPalette).where(ColorPalette.is_active.is_(True), ColorPalette.id != model.id)`.
- `on_model_delete`: reject deleting the last active color with the same count query.
- Raise `ConflictError` with messages `"Palette color slug is immutable"` and `"Cannot deactivate the last active palette color"` / `"Cannot delete the last active palette color"`.

Register `ColorPaletteAdmin` before the auth views in `setup_admin`. Update the tests in `tests/test_admin.py` that assert exactly three views to include `"color-palette"` as the first identity.

- [ ] **Step 5: Verify the migration renders without a database**

Run: `poetry run alembic upgrade head --sql > /tmp/milly-migration.sql`
Expected: exit code 0; the output contains `CREATE TABLE color_palette`, `INSERT INTO color_palette`, and `CREATE TABLE reminder_deliveries`.

- [ ] **Step 6: Run the tests and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add alembic/versions/0003_task_management.py src/backend/admin tests/test_task_management_migration.py tests/test_admin_views.py tests/test_admin.py
git commit -m "feat: add task management migration and palette admin"
```

---

### Task 4: Repository port and palette/project/label services

**Files:**
- Create: `src/backend/task_management/ports.py`
- Create: `src/backend/task_management/repository.py`
- Create: `src/backend/task_management/palette.py`
- Create: `src/backend/task_management/projects.py`
- Create: `src/backend/task_management/labels.py`
- Test: `tests/test_task_management_palette.py`
- Test: `tests/test_task_management_projects.py`
- Test: `tests/test_task_management_labels.py`

**Interfaces:**
- Consumes: models from Task 2; schedule primitives from Task 1.
- Produces:
  - `TaskManagementRepository` protocol in `ports.py` with the methods listed below.
  - `SqlAlchemyTaskManagementRepository(session: AsyncSession)`.
  - `PaletteService(repository)`.
  - `ProjectService(repository, palette_service, clock: Callable[[], datetime] | None = None)`.
  - `LabelService(repository, palette_service, clock: Callable[[], datetime] | None = None)`.
  - `ProjectProgress(total: int, completed: int, remaining: int)` dataclass and `ProjectSummary(project: Project, progress: ProjectProgress, color: ColorPalette)` dataclass.
  - `ColorSelectionError` is not introduced; use `BadRequestError` and `ConflictError` from `backend.core.errors`.

- [ ] **Step 1: Write the failing service tests**

Cover at least these behaviors:
- palette lists only active colors sorted by `(sort_order, id)`;
- omitted `color_id` selects the first active color;
- an inactive color is rejected with `bad_request`;
- deactivating or deleting the last active color raises `ConflictError`;
- project creation trims the name, rejects an empty name, rejects a duplicate active name, and defaults the color;
- project update cannot move to an inactive color;
- archiving hides the project from active lists but keeps it available with `include_archived=True`;
- unarchiving a name that conflicts with an active project raises `ConflictError`;
- deleting a project sets `deletion_batch_id`, soft-deletes its series and tasks, and asks the repository to cancel deliveries;
- label CRUD enforces per-user name uniqueness and soft delete is idempotent;
- cross-user project and label lookups raise `NotFoundError`.

Use `AsyncMock(spec=TaskManagementRepository)` for repository doubles and pass a fixed `now` into services.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_task_management_palette.py tests/test_task_management_projects.py tests/test_task_management_labels.py -q`
Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Define the repository port**

`TaskManagementRepository` must include at least:

```python
async def list_palette_colors(self) -> Sequence[ColorPalette]: ...
async def list_active_palette_colors(self) -> Sequence[ColorPalette]: ...
async def get_palette_color(self, color_id: int) -> ColorPalette | None: ...
async def get_default_palette_color(self) -> ColorPalette | None: ...
async def count_active_palette_colors(self) -> int: ...
async def palette_color_is_used(self, color_id: int) -> bool: ...
async def list_projects(self, user_id: int, *, include_archived: bool) -> Sequence[Project]: ...
async def get_project(self, user_id: int, project_id: int, *, for_update: bool = False) -> Project | None: ...
async def project_name_exists(self, user_id: int, name: str, *, exclude_project_id: int | None = None) -> bool: ...
async def add_project(self, project: Project) -> Project: ...
async def count_projects(self, user_id: int, *, archived: bool) -> int: ...
async def project_progress(self, user_id: int, project_id: int) -> tuple[int, int]: ...
async def soft_delete_project(self, project: Project, batch_id: UUID, deleted_at: datetime) -> None: ...
async def list_labels(self, user_id: int) -> Sequence[Label]: ...
async def get_label(self, user_id: int, label_id: int, *, for_update: bool = False) -> Label | None: ...
async def get_labels_by_ids(self, user_id: int, label_ids: Sequence[int]) -> Sequence[Label]: ...
async def label_name_exists(self, user_id: int, name: str, *, exclude_label_id: int | None = None) -> bool: ...
async def add_label(self, label: Label) -> Label: ...
async def soft_delete_label(self, label: Label, deleted_at: datetime) -> None: ...
```

The protocol docstrings must state that methods flush but never commit. `SqlAlchemyTaskManagementRepository` implements them with `select`, `func.lower`, `with_for_update`, `update`, and `flush`. `soft_delete_project` updates the project, its non-deleted series, its non-deleted tasks, and cancels `pending`/`failed`/`sending` deliveries in the same transaction. `list_active_palette_colors` orders by `(sort_order, id)`.

- [ ] **Step 4: Implement the services**

`PaletteService`:
- `list_active() -> Sequence[ColorPalette]`.
- `resolve_for_write(color_id: int | None) -> ColorPalette`: returns the default active color when `None`; raises `BadRequestError` when the color is missing or inactive.
- `ensure_active(color_id: int) -> ColorPalette`: same without the default fallback.
- `ensure_can_deactivate(color_id: int) -> None` and `ensure_can_delete(color_id: int) -> None`: raise `ConflictError` when the count of other active colors is zero.

`ProjectService`:
- `list(user_id, include_archived: bool) -> list[ProjectSummary]`.
- `get(user_id, project_id) -> Project`.
- `create(user_id, *, name, description, color_id, icon) -> Project`.
- `update(user_id, project_id, *, name=None, description=..., color_id=None, icon=...) -> Project`.
- `archive(user_id, project_id) -> Project`.
- `unarchive(user_id, project_id) -> Project`.
- `delete(user_id, project_id) -> None`.
- `stats(user_id) -> dict[str, int]` with keys `active`, `archived`, `planned`.
- Names are stripped; empty names raise `BadRequestError`.
- Duplicate active names raise `ConflictError("Project name already exists")`.

`LabelService`:
- `list(user_id)`, `get(user_id, label_id)`, `create`, `update`, `delete`.
- Same name/color rules as projects; deleting is idempotent and returns `None`.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `poetry run pytest tests/test_task_management_palette.py tests/test_task_management_projects.py tests/test_task_management_labels.py -q`
Expected: all tests pass.

- [ ] **Step 6: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/task_management tests/test_task_management_palette.py tests/test_task_management_projects.py tests/test_task_management_labels.py
git commit -m "feat: add palette project and label services"
```

---

### Task 5: Task, series, and reminder services

**Files:**
- Create: `src/backend/task_management/tasks.py`
- Create: `src/backend/task_management/series.py`
- Create: `src/backend/task_management/reminders.py`
- Modify: `src/backend/task_management/ports.py`
- Modify: `src/backend/task_management/repository.py`
- Test: `tests/test_task_management_tasks.py`
- Test: `tests/test_task_management_series.py`
- Test: `tests/test_task_management_reminders.py`

**Interfaces:**
- Consumes: Task 1 scheduling functions, Task 4 `TaskManagementRepository`, `ProjectService`, `LabelService`, `PaletteService`.
- Produces:
  - `TaskFilters` dataclass: `date: date | None`, `project_ids: tuple[int, ...]`, `priorities: tuple[Priority, ...]`, `status: Literal["all", "open", "completed", "overdue"]`, `recurrence: Literal["all", "regular", "one_off"]`, `query: str | None`, `include_archived_projects: bool = False`.
  - `TaskDetail` dataclass: `task: Task`, `labels: Sequence[Label]`, `reminders: Sequence[TaskReminder]`, `project: Project | None`.
  - `ReminderInput` dataclass: `kind: ReminderKind`, `enabled: bool = True`, `offset_minutes: int | None = None`, `interval_days: int | None = None`, `time_of_day: time | None = None`.
  - `SeriesService(repository, project_service, label_service, clock: Callable[[], datetime] | None = None)`.
  - `TaskService(repository, series_service: SeriesService, clock: Callable[[], datetime] | None = None)`.
  - `ReminderDispatcher(repository, sender, clock: Callable[[], datetime] | None = None)` with `dispatch_due() -> int` returning the number of successfully sent deliveries.
  - Methods listed in Step 4.

- [ ] **Step 1: Write the failing tests**

Cover at least:
- creating a one-off task inserts the task, labels, and one `before_due` reminder with `next_run_at = due_at - offset`;
- a past `before_due` moment is skipped;
- a one-off task with `scheduled` gets the first slot from the due date, advancing past `now` when needed;
- creating a series with `starts_on` in the past starts today or tomorrow and does not backfill;
- creating a series with a future `starts_on` beyond the horizon creates exactly one instance;
- generation copies title, description, priority, project, and label IDs into instances;
- generation is idempotent when the repository's slot set already contains a due time;
- editing a series updates only future, incomplete, non-deleted instances and does not touch past or completed ones;
- completing a task sets `completed_at` and cancels pending deliveries;
- uncompleting a task clears `completed_at` and rebuilds reminders from `now`;
- deleting a one-off task soft-deletes it and cancels deliveries;
- deleting a series soft-deletes future incomplete instances and keeps past instances;
- archiving a project nulls reminder `next_run_at` and cancels pending deliveries for its tasks;
- the dispatcher creates a delivery keyed by `(reminder_id, scheduled_for)`, skips an already sent key, claims a row, sends, marks it sent, and advances `next_run_at`;
- a sender failure marks `failed` with backoff and leaves `locked_at` unset;
- `before_due` clears `next_run_at` after final success or final failure.

Use a fake sender object with `async def send(self, user_id: int, text: str) -> int | None`.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py tests/test_task_management_series.py tests/test_task_management_reminders.py -q`
Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Extend the repository port and implementation**

Add methods for:
- task CRUD, task list filters, labels/reminder loading, series CRUD, series generation queries;
- `get_series_tasks_for_update(series_id, user_id)`, `list_future_series_tasks(series_id, user_id, now)`;
- reminder CRUD, delivery create/get/claim/mark, cancel/reactivate;
- `project_is_active(user_id, project_id)`;
- `today_progress(user_id, start, end)`, `planned_tasks_count(user_id)`.

All methods flush but never commit, except explicit `commit()` calls used by the dispatcher after claiming a delivery. The claim method must use `with_for_update(skip_locked=True)` on `task_reminders` and return the locked reminder or `None`.

- [ ] **Step 4: Implement the services**

`TaskService`:
- `list(user_id, filters) -> list[TaskDetail]`.
- `get(user_id, task_id) -> TaskDetail`.
- `create(user_id, *, title, description, priority, due_at, project_id, label_ids, reminders_enabled, reminders) -> TaskDetail`.
- `update(user_id, task_id, ...) -> TaskDetail`: when the task has `series_id`, treat the request as a series update and apply it to future incomplete instances.
- `complete(user_id, task_id) -> TaskDetail`.
- `uncomplete(user_id, task_id) -> TaskDetail`.
- `delete(user_id, task_id) -> None`: when the task has `series_id`, treat the request as a series delete.

`SeriesService`:
- `list(user_id)`, `get(user_id, series_id)`.
- `create(user_id, *, title, description, priority, time_of_day, timezone, starts_on, ends_on, project_id, label_ids, reminders_enabled, reminders) -> TaskSeries`.
- `update(user_id, series_id, ...) -> TaskSeries`.
- `delete(user_id, series_id) -> None`.
- `generate(now: datetime) -> int` for the Taskiq generation job; selects active series with `generated_through < now + 90 days`, generates missing slots, copies labels, writes `TaskReminder` rows for instances with computed `next_run_at`, and updates `generated_through`. It must not generate for deleted series or archived/deleted projects.

`ReminderDispatcher`:
- `dispatch_due()` selects due enabled reminders whose master toggle is on and whose task/series is active and whose project is not archived;
- for each, creates a delivery with `scheduled_for = reminder.next_run_at`; if a `sent` delivery already exists for the key, only advances `next_run_at`; if a `cancelled` delivery exists, reactivate it to `pending`;
- claims the delivery (`status='sending'`, `locked_at=now`, `attempts += 1`) and commits;
- sends Telegram text `"Напоминание: {title}"`;
- on success sets `sent`, `sent_at`, `telegram_message_id`, clears `locked_at`, and advances `next_run_at`;
- on failure sets `pending` with `next_attempt_at` backoff until `attempts >= 5`, then `failed`; both clear `locked_at` and advance the slot;
- `before_due` sets `next_run_at = None` after final success or final failure.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `poetry run pytest tests/test_task_management_tasks.py tests/test_task_management_series.py tests/test_task_management_reminders.py -q`
Expected: all tests pass.

- [ ] **Step 6: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/task_management tests/test_task_management_tasks.py tests/test_task_management_series.py tests/test_task_management_reminders.py
git commit -m "feat: add task series and reminder services"
```

---

### Task 6: Taskiq generation and delivery jobs; remove example tasks

**Files:**
- Create: `src/backend/tasks/generation.py`
- Create: `src/backend/tasks/delivery.py`
- Create: `src/backend/tasks/telegram.py`
- Delete: `src/backend/tasks/example.py`
- Delete: `src/backend/api/routes/tasks.py`
- Modify: `src/backend/tasks/worker.py`
- Modify: `src/backend/api/router.py`
- Modify: `src/backend/main.py`
- Modify: `src/backend/api/schemas.py`
- Modify: `compose.yml`
- Modify: `README.md`
- Modify: `tests/test_tasks.py`
- Modify: `tests/test_tasks_api.py`
- Test: `tests/test_task_generation.py`
- Test: `tests/test_reminder_delivery.py`

**Interfaces:**
- Consumes: `SeriesService.generate`, `ReminderDispatcher`, `get_task_session`.
- Produces:
  - `generate_task_series` Taskiq task scheduled `{"cron": "*/15 * * * *", "schedule_id": "task-series-generation"}`.
  - `dispatch_task_reminders` Taskiq task scheduled `{"cron": "* * * * *", "schedule_id": "reminder-delivery"}`.
  - `TelegramSender` protocol and `TelegramBotSender(settings)`.

- [ ] **Step 1: Write the failing job tests**

```python
from backend.tasks.delivery import dispatch_task_reminders
from backend.tasks.generation import generate_task_series


def test_generation_task_has_schedule() -> None:
    assert generate_task_series.labels["schedule"][0]["schedule_id"] == "task-series-generation"


def test_delivery_task_has_schedule() -> None:
    assert dispatch_task_reminders.labels["schedule"][0]["schedule_id"] == "reminder-delivery"
```

Also add behavioral tests that monkeypatch the service class with a fake and assert the task body calls `generate`/`dispatch_due`. Update `tests/test_tasks.py` to keep only broker and middleware tests; delete the heartbeat and ping assertions. Delete `tests/test_tasks_api.py` entirely.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_tasks.py tests/test_task_generation.py tests/test_reminder_delivery.py -q`
Expected: FAIL because the new modules and schedules do not exist.

- [ ] **Step 3: Implement the Taskiq jobs and Telegram sender**

`src/backend/tasks/telegram.py`:
- `TelegramSender` protocol with `async def send(self, user_id: int, text: str) -> int | None: ...`.
- `TelegramBotSender(settings)`: reads `settings.telegram_bot_token`; when missing, raises `AuthNotConfiguredError` from `backend.auth.errors`.
- Use `urllib.parse.urlencode`, `urllib.request.Request`, and `asyncio.to_thread` to POST to `https://api.telegram.org/bot<token>/sendMessage`. Parse the JSON `ok`/`result.message_id`; raise a `RuntimeError` with a safe message on failure. Never log the token or message text.

`src/backend/tasks/generation.py`:
- import `broker`; define the scheduled task with `TaskiqDepends(get_task_session)`; construct `SqlAlchemyTaskManagementRepository` and `SeriesService`; call `await service.generate(datetime.now(UTC))`; log `task.series_generated` with the count.

`src/backend/tasks/delivery.py`:
- same dependency pattern; construct `ReminderDispatcher(repository, TelegramBotSender(settings), clock=lambda: datetime.now(UTC))`; call `await dispatcher.dispatch_due()`; log `task.reminders_dispatched` with the count.

`worker.py` imports `backend.tasks.generation` and `backend.tasks.delivery` instead of `backend.tasks.example`.

- [ ] **Step 4: Remove the example tasks and ping endpoint**

Delete `backend/tasks/example.py`, `backend/api/routes/tasks.py`, and `tests/test_tasks_api.py`. Remove the tasks router include from `backend/api/router.py`. Remove `TaskEnqueuedResponse` from `backend/api/schemas.py`. Update `main.py` docstrings that mention example tasks. Update `compose.yml` worker and scheduler module lists to load `backend.tasks.generation` and `backend.tasks.delivery`. Update the README task section to describe generation and delivery jobs instead of ping/heartbeat; remove the `POST /api/v1/tasks/ping` line and the `backend.tasks.example` commands.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `poetry run pytest -q`
Expected: all tests pass and no test references `ping`, `heartbeat`, or `backend.tasks.example`.

- [ ] **Step 6: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/tasks src/backend/api src/backend/main.py compose.yml README.md tests
git commit -m "feat: add taskiq generation and reminder delivery"
```

---

### Task 7: Palette, project, and label API

**Files:**
- Create: `src/backend/api/task_schemas.py`
- Create: `src/backend/api/routes/color_palette.py`
- Create: `src/backend/api/routes/projects.py`
- Create: `src/backend/api/routes/labels.py`
- Modify: `src/backend/api/deps.py`
- Modify: `src/backend/api/router.py`
- Test: `tests/test_color_palette_api.py`
- Test: `tests/test_projects_api.py`
- Test: `tests/test_labels_api.py`

**Interfaces:**
- Consumes: Task 4 services.
- Produces: `GET /api/v1/color-palette`, project CRUD/archive/delete routes, label CRUD routes; Pydantic schemas.

- [ ] **Step 1: Write the failing API tests**

Use `app.dependency_overrides` to replace the service dependencies with async fakes. Cover:
- palette response contains only active colors and preserves `(sort_order, id)` order;
- project list includes progress fields `total`, `completed`, `remaining` and color data;
- creating a project defaults `color_id`;
- duplicate project name returns `409 conflict` with the unified envelope and `request_id`;
- another user's project returns `404 not_found`;
- archiving and unarchiving change the active-list visibility;
- deleting a project returns `204`;
- label create/update/delete and duplicate-name conflict;
- all protected routes require a bearer token and return `401 unauthorized` without one.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_color_palette_api.py tests/test_projects_api.py tests/test_labels_api.py -q`
Expected: FAIL because the routes do not exist.

- [ ] **Step 3: Add schemas and dependencies**

In `backend/api/task_schemas.py` add:
- `ColorResponse(id, slug, name, hex)` and `ColorPaletteResponse(items: list[ColorResponse])`.
- `ProjectCreate(name, description=None, color_id=None, icon=None)`, `ProjectUpdate(name=None, description=..., color_id=None, icon=...)`, `ProjectProgressResponse(total, completed, remaining)`, `ProjectResponse(id, name, description, icon, color, progress, archived_at, created_at, updated_at)`, `ProjectListResponse(items)`.
- `LabelCreate(name, color_id=None)`, `LabelUpdate(name=None, color_id=None)`, `LabelResponse(id, name, color, created_at, updated_at)`, `LabelListResponse(items)`.
- Validators trim names and reject empty values; `color_id` uses `gt=0`.

In `backend/api/deps.py` add:
- `get_palette_service(session: SessionDep) -> PaletteService`
- `get_project_service(session: SessionDep) -> ProjectService`
- `get_label_service(session: SessionDep) -> LabelService`
- annotated dependency aliases `PaletteServiceDep`, `ProjectServiceDep`, `LabelServiceDep`.

- [ ] **Step 4: Implement the routes**

- `GET /api/v1/color-palette` → `ColorPaletteResponse`; tags `color-palette`.
- `GET /api/v1/projects?include_archived=false` → `ProjectListResponse`.
- `POST /api/v1/projects` → 201 `ProjectResponse`.
- `GET /api/v1/projects/{project_id}` → `ProjectResponse`.
- `PATCH /api/v1/projects/{project_id}` → `ProjectResponse`.
- `POST /api/v1/projects/{project_id}/archive` and `/unarchive` → `ProjectResponse`.
- `DELETE /api/v1/projects/{project_id}` → 204.
- `GET /api/v1/labels`, `POST /api/v1/labels` (201), `PATCH /api/v1/labels/{label_id}`, `DELETE /api/v1/labels/{label_id}` (204).

Every route uses `StandardizedAPIRouter` and `document_errors` for `NOT_FOUND`, `CONFLICT`, `BAD_REQUEST`, and `UNAUTHORIZED`. All routes depend on `CurrentAccessDep` and pass `claims.user_id` to services. Do not return deleted entities.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `poetry run pytest tests/test_color_palette_api.py tests/test_projects_api.py tests/test_labels_api.py -q`
Expected: all tests pass.

- [ ] **Step 6: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/api tests/test_color_palette_api.py tests/test_projects_api.py tests/test_labels_api.py
git commit -m "feat: add palette project and label api"
```

---

### Task 8: Task, series, and timezone API

**Files:**
- Modify: `src/backend/api/task_schemas.py`
- Create: `src/backend/api/routes/task_series.py`
- Rewrite: `src/backend/api/routes/tasks.py`
- Modify: `src/backend/api/routes/auth.py`
- Modify: `src/backend/api/deps.py`
- Modify: `src/backend/api/router.py`
- Modify: `src/backend/auth/service.py`
- Modify: `src/backend/auth/repository.py`
- Modify: `src/backend/auth/schemas.py`
- Modify: `tests/test_auth_api.py`
- Test: `tests/test_tasks_api.py`
- Test: `tests/test_task_series_api.py`

**Interfaces:**
- Consumes: Task 5 services, `AuthService`.
- Produces:
  - `GET/POST/PATCH/DELETE /api/v1/tasks`, `/tasks/{id}`, `/tasks/{id}/complete`, `/tasks/{id}/uncomplete`.
  - `GET/POST/PATCH/DELETE /api/v1/task-series`, `/task-series/{id}`.
  - `PATCH /api/v1/auth/me/timezone`.
  - `UserResponse.timezone`.

- [ ] **Step 1: Write the failing API tests**

Cover:
- task creation returns the reminder configuration and computed `next_run_at`;
- task list filters by `date`, `project_ids`, `priorities`, `status`, `recurrence`, and `q`;
- completing and uncompleting a task return the updated task;
- editing a task that belongs to a series returns the updated series semantics (the fake service records the series path);
- deleting a task returns `204`;
- series creation returns the first generated instance metadata and schedule;
- series update and delete call the series service;
- another user's task/series returns `404 not_found`;
- `PATCH /api/v1/auth/me/timezone` validates the IANA zone, calls `AuthService.update_timezone`, and returns the updated user; an invalid zone returns `422 validation_error`;
- `UserResponse` includes `timezone`.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `poetry run pytest tests/test_tasks_api.py tests/test_task_series_api.py tests/test_auth_api.py -q`
Expected: FAIL because the routes and timezone service do not exist.

- [ ] **Step 3: Implement the task and series API**

Add schemas:
- `ReminderInputSchema(kind: Literal["before_due", "scheduled"], enabled: bool = True, offset_minutes: int | None = None, interval_days: int | None = None, time_of_day: time | None = None)` with a model validator enforcing the same rules as the database checks.
- `ReminderResponse(id, kind, enabled, offset_minutes, interval_days, time_of_day, timezone, next_run_at)`.
- `TaskCreate(title, description=None, priority: Priority = Priority.NEITHER, due_at: datetime, project_id=None, label_ids=[], reminders_enabled=False, reminders=[])`.
- `TaskUpdate` with optional fields and reminders; `TaskResponse(id, series_id, project_id, title, description, priority, due_at, completed_at, reminders_enabled, labels, reminders, created_at, updated_at)`.
- `TaskListResponse(items)`, `TaskFiltersQuery` dependency for query parameters.
- `SeriesCreate(title, description=None, priority, time_of_day, timezone, starts_on, ends_on=None, project_id=None, label_ids=[], reminders_enabled=False, reminders=[])`.
- `SeriesUpdate`, `SeriesResponse(id, title, description, priority, frequency, time_of_day, timezone, starts_on, ends_on, generated_through, reminders_enabled, labels, reminders, created_at, updated_at)`.
- Validate `due_at` is timezone-aware (`utcoffset() is not None`) and `timezone` is a valid IANA zone via `ZoneInfo`; invalid values become `ValidationError` so the existing 422 envelope applies.

Add dependencies:
- `get_task_service(session: SessionDep) -> TaskService`
- `get_series_service(session: SessionDep) -> SeriesService`
- `TaskServiceDep`, `SeriesServiceDep`.

Implement routes with `CurrentAccessDep`. `PATCH /tasks/{task_id}` must accept the same fields as `TaskUpdate`; when the task has a series, the service applies the update to future instances and the response is the representative instance or series detail as defined by the service. `POST /tasks/{task_id}/complete` and `/uncomplete` return the updated `TaskResponse`. `DELETE` returns 204.

- [ ] **Step 4: Add the timezone profile endpoint**

- Add `timezone: str` to `UserResponse`.
- Add `TimezoneUpdate(timezone: str)` to `backend/auth/schemas.py` with a `field_validator` using `ZoneInfo`.
- Add `AuthService.update_timezone(user_id: int, timezone: str) -> User` that validates the zone, updates `User.timezone`, and returns the user; it must not commit.
- Add `AuthRepository.update_user_timezone(user_id, timezone)` to the protocol and the SQLAlchemy implementation.
- Add `PATCH /api/v1/auth/me/timezone` to `backend/api/routes/auth.py` using `CurrentUserDep`; return `UserResponse`.
- Update existing auth tests and any `UserResponse` fixture to include `timezone`.

- [ ] **Step 5: Run the tests and verify they pass**

Run: `poetry run pytest tests/test_tasks_api.py tests/test_task_series_api.py tests/test_auth_api.py -q`
Expected: all tests pass.

- [ ] **Step 6: Run the project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
git add src/backend/api src/backend/auth tests/test_tasks_api.py tests/test_task_series_api.py tests/test_auth_api.py
git commit -m "feat: add task series and timezone api"
```

---

### Task 9: Documentation, final integration verification, and cleanup

**Files:**
- Modify: `README.md`
- Modify: `.env.example` if any new settings are introduced (none expected)
- Test: `tests/test_task_management_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: documented endpoints and a final verification run.

- [ ] **Step 1: Add an integration-style test over the service composition**

Write `tests/test_task_management_integration.py` using the fake repository and fake sender to prove one complete flow without PostgreSQL:
1. create an active palette color and a project;
2. create a daily series;
3. generate instances;
4. complete one instance and verify only that instance changes;
5. dispatch a due reminder and verify one sent delivery;
6. dispatch the same reminder again and verify no duplicate delivery;
7. archive the project and verify generation and delivery are paused.
8. soft-delete the project and verify its tasks are hidden and pending deliveries are cancelled.

- [ ] **Step 2: Update README**

Document the new endpoints, the daily-only recurrence rule, the 90-day horizon, the palette endpoint, and the taskiq job names. Remove all remaining `ping`/`heartbeat` references.

- [ ] **Step 3: Run the full verification suite**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
poetry run alembic upgrade head --sql > /tmp/milly-final-migration.sql
```

Expected: every command exits 0; the migration SQL contains the nine new tables and the 15 seed inserts.

- [ ] **Step 4: Commit**

```bash
git add README.md tests/test_task_management_integration.py
git commit -m "test: cover task management flows end to end"
```
