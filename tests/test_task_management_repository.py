from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.project import Project
from backend.task_management.repository import SqlAlchemyTaskManagementRepository

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
USER_ID = 7
PROJECT_ID = 5
BATCH_ID = UUID("11111111-1111-1111-1111-111111111111")


class RecordingScalars:
    """Scalar collection returned by the recording result."""

    def __init__(self, values: Sequence[Any]) -> None:
        """
        Store the scalar values to return.

        Purpose:
            Give repository callers an iterable result without a database.

        Args:
            values: Values returned by ``all()``.
        """
        self._values = list(values)

    def all(self) -> list[Any]:
        """
        Return every recorded scalar value.

        Purpose:
            Match the SQLAlchemy ``ScalarResult`` contract used by the
            repository.

        Returns:
            list[Any]: Stored scalar values.
        """
        return self._values


class RecordingResult:
    """Configurable result returned by the recording session."""

    def __init__(
        self,
        *,
        values: Sequence[Any] = (),
        scalar: Any = None,
        row: tuple[int, int] = (0, 0),
    ) -> None:
        """
        Configure the values returned by each result accessor.

        Purpose:
            Let each repository test provide only the result shape it reads.

        Args:
            values: Values returned through ``scalars().all()``.
            scalar: Value returned by ``scalar_one`` and
                ``scalar_one_or_none``.
            row: Row returned by ``one``.
        """
        self._values = list(values)
        self._scalar = scalar
        self._row = row

    def scalars(self) -> RecordingScalars:
        """
        Return the scalar collection.

        Purpose:
            Support list-returning repository methods.

        Returns:
            RecordingScalars: Configured scalar values.
        """
        return RecordingScalars(self._values)

    def scalar_one(self) -> Any:
        """
        Return the configured scalar value.

        Purpose:
            Support count and boolean repository methods.

        Returns:
            Any: Configured scalar value.
        """
        return self._scalar

    def scalar_one_or_none(self) -> Any:
        """
        Return the configured optional scalar value.

        Purpose:
            Support lookup and existence repository methods.

        Returns:
            Any: Configured scalar value or ``None``.
        """
        return self._scalar

    def one(self) -> tuple[int, int]:
        """
        Return the configured row.

        Purpose:
            Support progress-count repository methods.

        Returns:
            tuple[int, int]: Configured totals.
        """
        return self._row


class RecordingSession:
    """Async session double that records statements without a database."""

    def __init__(self, results: Sequence[RecordingResult] | None = None) -> None:
        """
        Initialize the recording session.

        Purpose:
            Capture SQLAlchemy statements and return prepared results in call
            order.

        Args:
            results: Results to return from successive ``execute`` calls.
        """
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flush_count = 0
        self._results = list(results or [])

    async def execute(self, statement: Any) -> RecordingResult:
        """
        Record a statement and return the next prepared result.

        Purpose:
            Let repository tests inspect exactly what would be sent to
            PostgreSQL.

        Args:
            statement: SQLAlchemy statement passed by the repository.

        Returns:
            RecordingResult: The next prepared result or an empty default.
        """
        self.statements.append(statement)
        if self._results:
            return self._results.pop(0)
        return RecordingResult()

    def add(self, instance: Any) -> None:
        """
        Record an object added to the session.

        Purpose:
            Verify insert methods add their entity before flushing.

        Args:
            instance: ORM object passed to ``add``.
        """
        self.added.append(instance)

    async def flush(self) -> None:
        """
        Record a flush.

        Purpose:
            Verify write methods flush without committing.

        Returns:
            None: No database work is performed.
        """
        self.flush_count += 1


def make_repository(session: RecordingSession) -> SqlAlchemyTaskManagementRepository:
    """Build a repository bound to the recording session."""
    return SqlAlchemyTaskManagementRepository(cast(AsyncSession, session))


def compile_statement(statement: Any) -> str:
    """Compile one recorded statement with literal binds for PostgreSQL."""
    return str(
        statement.compile(
            dialect=cast(Any, postgresql.dialect)(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def test_list_projects_filters_and_orders_active_projects() -> None:
    """Active project listing is tenant-scoped, non-deleted, and deterministic."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_projects(USER_ID, include_archived=False)
    active_sql = compile_statement(session.statements[-1])

    await repository.list_projects(USER_ID, include_archived=True)
    all_sql = compile_statement(session.statements[-1])

    assert "projects.user_id = 7" in active_sql
    assert "projects.deleted_at IS NULL" in active_sql
    assert "projects.archived_at IS NULL" in active_sql
    assert (
        "ORDER BY projects.archived_at ASC NULLS FIRST, lower(projects.name), projects.id"
        in active_sql
    )
    assert "projects.user_id = 7" in all_sql
    assert "projects.deleted_at IS NULL" in all_sql
    assert "projects.archived_at IS NULL" not in all_sql
    assert (
        "ORDER BY projects.archived_at ASC NULLS FIRST, lower(projects.name), projects.id"
        in all_sql
    )


async def test_project_name_exists_is_case_insensitive_active_and_excludes_id() -> None:
    """Project name checks compare lower-case active names and skip the current id."""
    session = RecordingSession(
        results=[
            RecordingResult(scalar=1),
            RecordingResult(scalar=None),
        ]
    )
    repository = make_repository(session)

    exists = await repository.project_name_exists(USER_ID, "Work", exclude_project_id=PROJECT_ID)
    without_exclusion = await repository.project_name_exists(USER_ID, "Work")

    excluded_sql = compile_statement(session.statements[0])
    plain_sql = compile_statement(session.statements[1])
    assert exists is True
    assert without_exclusion is False
    assert "lower(projects.name) = 'work'" in excluded_sql
    assert "projects.deleted_at IS NULL" in excluded_sql
    assert "projects.archived_at IS NULL" in excluded_sql
    assert "projects.id != 5" in excluded_sql
    assert "LIMIT 1" in excluded_sql
    assert "lower(projects.name) = 'work'" in plain_sql
    assert "projects.id !=" not in plain_sql


async def test_label_name_exists_is_case_insensitive_non_deleted_and_excludes_id() -> None:
    """Label name checks compare lower-case non-deleted names and skip the current id."""
    session = RecordingSession(
        results=[
            RecordingResult(scalar=1),
            RecordingResult(scalar=None),
        ]
    )
    repository = make_repository(session)

    exists = await repository.label_name_exists(USER_ID, "Home", exclude_label_id=9)
    without_exclusion = await repository.label_name_exists(USER_ID, "Home")

    excluded_sql = compile_statement(session.statements[0])
    plain_sql = compile_statement(session.statements[1])
    assert exists is True
    assert without_exclusion is False
    assert "lower(labels.name) = 'home'" in excluded_sql
    assert "labels.user_id = 7" in excluded_sql
    assert "labels.deleted_at IS NULL" in excluded_sql
    assert "labels.id != 9" in excluded_sql
    assert "labels.archived_at" not in excluded_sql
    assert "lower(labels.name) = 'home'" in plain_sql
    assert "labels.id !=" not in plain_sql


async def test_get_project_and_label_are_tenant_scoped_and_can_lock() -> None:
    """Project and label lookups filter by owner, exclude deleted rows, and lock on demand."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.get_project(USER_ID, PROJECT_ID)
    project_sql = compile_statement(session.statements[-1])

    await repository.get_project(USER_ID, PROJECT_ID, for_update=True)
    locked_project_sql = compile_statement(session.statements[-1])

    await repository.get_label(USER_ID, 9)
    label_sql = compile_statement(session.statements[-1])

    await repository.get_label(USER_ID, 9, for_update=True)
    locked_label_sql = compile_statement(session.statements[-1])

    assert "projects.id = 5" in project_sql
    assert "projects.user_id = 7" in project_sql
    assert "projects.deleted_at IS NULL" in project_sql
    assert "FOR UPDATE" not in project_sql
    assert "FOR UPDATE" in locked_project_sql
    assert "labels.id = 9" in label_sql
    assert "labels.user_id = 7" in label_sql
    assert "labels.deleted_at IS NULL" in label_sql
    assert "FOR UPDATE" not in label_sql
    assert "FOR UPDATE" in locked_label_sql


async def test_list_active_palette_colors_filters_and_orders() -> None:
    """Active palette listing excludes inactive colors and orders deterministically."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_active_palette_colors()
    sql = compile_statement(session.statements[-1])

    assert "color_palette.is_active IS true" in sql
    assert "ORDER BY color_palette.sort_order, color_palette.id" in sql


async def test_get_default_palette_color_is_first_active() -> None:
    """The default palette color is the first active color by display order."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_repository(session)

    await repository.get_default_palette_color()
    sql = compile_statement(session.statements[-1])

    assert "color_palette.is_active IS true" in sql
    assert "ORDER BY color_palette.sort_order, color_palette.id" in sql
    assert "LIMIT 1" in sql


async def test_soft_delete_project_emits_owner_scoped_cascade() -> None:
    """Project deletion updates the project tree and cancels active deliveries."""
    session = RecordingSession()
    repository = make_repository(session)
    project = Project(id=PROJECT_ID, user_id=USER_ID, name="Work", color_id=1)

    await repository.soft_delete_project(project, BATCH_ID, NOW)

    project_sql, series_sql, tasks_sql, reminders_sql, deliveries_sql = (
        compile_statement(statement) for statement in session.statements
    )
    assert len(session.statements) == 5
    assert session.flush_count == 1

    assert project_sql.startswith("UPDATE projects SET deleted_at=")
    assert "deletion_batch_id=" in project_sql
    assert str(BATCH_ID) in project_sql
    assert "projects.id = 5" in project_sql
    assert "projects.user_id = 7" in project_sql
    assert "projects.deleted_at IS NULL" in project_sql

    assert series_sql.startswith("UPDATE task_series SET deleted_at=")
    assert "deletion_batch_id=" in series_sql
    assert "task_series.project_id = 5" in series_sql
    assert "task_series.user_id = 7" in series_sql
    assert "task_series.deleted_at IS NULL" in series_sql

    assert tasks_sql.startswith("UPDATE tasks SET deleted_at=")
    assert "deletion_batch_id=" in tasks_sql
    assert "tasks.project_id = 5" in tasks_sql
    assert "tasks.user_id = 7" in tasks_sql
    assert "tasks.deleted_at IS NULL" in tasks_sql

    assert reminders_sql.startswith("UPDATE task_reminders SET next_run_at=NULL")
    assert "task_reminders.user_id = 7" in reminders_sql
    assert "task_reminders.task_id IN (SELECT tasks.id" in reminders_sql
    assert "task_reminders.series_id IN (SELECT task_series.id" in reminders_sql

    assert deliveries_sql.startswith("UPDATE reminder_deliveries SET status='cancelled'")
    assert "locked_at=NULL" in deliveries_sql
    assert "next_attempt_at=NULL" in deliveries_sql
    assert "reminder_deliveries.user_id = 7" in deliveries_sql
    assert "reminder_deliveries.status IN ('pending', 'failed', 'sending')" in deliveries_sql
    assert "'sent'" not in deliveries_sql


async def test_planned_tasks_count_excludes_inactive_plans_and_completed_tasks() -> None:
    """Planned tasks exclude deleted, completed, and inactive-project tasks."""
    session = RecordingSession(results=[RecordingResult(scalar=4)])
    repository = make_repository(session)

    result = await repository.planned_tasks_count(USER_ID)
    sql = compile_statement(session.statements[-1])

    assert result == 4
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.completed_at IS NULL" in sql
    assert "tasks.project_id IS NULL" in sql
    assert "tasks.project_id IN (SELECT projects.id" in sql
    assert "projects.deleted_at IS NULL" in sql
    assert "projects.archived_at IS NULL" in sql


async def test_project_progress_counts_all_and_completed_tasks() -> None:
    """Project progress counts non-deleted tasks and completion timestamps."""
    session = RecordingSession(results=[RecordingResult(row=(5, 2))])
    repository = make_repository(session)

    result = await repository.project_progress(USER_ID, PROJECT_ID)
    sql = compile_statement(session.statements[-1])

    assert result == (5, 2)
    assert "count(tasks.id)" in sql
    assert "FILTER (WHERE tasks.completed_at IS NOT NULL)" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.project_id = 5" in sql
    assert "tasks.deleted_at IS NULL" in sql


async def test_today_progress_bounds_due_at_and_counts_completed_tasks() -> None:
    """Today progress uses the due-at window and completion timestamps."""
    end = NOW.replace(hour=21, minute=0)
    session = RecordingSession(results=[RecordingResult(row=(3, 1))])
    repository = make_repository(session)

    result = await repository.today_progress(USER_ID, NOW, end)
    sql = compile_statement(session.statements[-1])

    assert result == (3, 1)
    assert "count(tasks.id)" in sql
    assert "FILTER (WHERE tasks.completed_at IS NOT NULL)" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.due_at >=" in sql
    assert "tasks.due_at <" in sql
    assert "tasks.deleted_at IS NULL" in sql


async def test_palette_color_is_used_checks_projects_and_labels() -> None:
    """Palette usage checks both project and label references."""
    session = RecordingSession(results=[RecordingResult(scalar=False)])
    repository = make_repository(session)

    result = await repository.palette_color_is_used(4)
    sql = compile_statement(session.statements[-1])

    assert result is False
    assert "EXISTS (SELECT projects.id" in sql
    assert "projects.color_id = 4" in sql
    assert "EXISTS (SELECT labels.id" in sql
    assert "labels.color_id = 4" in sql


async def test_claim_series_for_generation_uses_skip_locked() -> None:
    """Generation claims lock the tenant-scoped series row without blocking."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_repository(session)

    result = await repository.claim_series_for_generation(3, USER_ID)
    sql = compile_statement(session.statements[-1])

    assert result is None
    assert "task_series.id = 3" in sql
    assert "task_series.user_id = 7" in sql
    assert "task_series.deleted_at IS NULL" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
