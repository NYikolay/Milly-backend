from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import ColorPalette as ColorPaletteEntity
from backend.domain.entities import Project as ProjectEntity
from backend.infrastructure.persistence.sqlalchemy.account_repository import (
    SqlAlchemyAccountRepository,
)
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    color_to_entity,
    project_to_entity,
)
from backend.infrastructure.persistence.sqlalchemy.palette_repository import (
    SqlAlchemyPaletteRepository,
)
from backend.infrastructure.persistence.sqlalchemy.project_repository import (
    SqlAlchemyProjectRepository,
)
from backend.models.color_palette import ColorPalette
from backend.models.project import Project
from backend.models.task_label import TaskLabel
from backend.models.task_reminder import TaskReminder
from backend.task_management.repository import SqlAlchemyTaskManagementRepository
from tests.task_management_fakes import make_task

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

    def all(self) -> list[Any]:
        """
        Return every configured row.

        Purpose:
            Support row-returning batch repository methods.

        Returns:
            list[Any]: Configured row values.
        """
        return list(self._values)


class RecordingSession:
    """Async session double that records statements without a database."""

    def __init__(
        self,
        results: Sequence[RecordingResult] | None = None,
        *,
        get_results: Sequence[Any] | None = None,
    ) -> None:
        """
        Initialize the recording session.

        Purpose:
            Capture SQLAlchemy statements and return prepared results in call
            order.

        Args:
            results: Results to return from successive ``execute`` calls.
            get_results: ORM rows to return from successive primary-key
                lookups.
        """
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flush_count = 0
        self._results = list(results or [])
        self._get_results = list(get_results or [])

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

    async def get(self, entity: Any, ident: Any) -> Any:
        """
        Return the next prepared ORM row for a primary-key lookup.

        Purpose:
            Support save methods that load the tracked row before applying
            domain values.

        Args:
            entity: Mapped class requested by the repository.
            ident: Primary-key value requested by the repository.

        Returns:
            Any: The next prepared row, or ``None``.
        """
        if self._get_results:
            return self._get_results.pop(0)
        return None

    def add(self, instance: Any) -> None:
        """
        Record an object added to the session.

        Purpose:
            Verify insert methods add their entity before flushing.

        Args:
            instance: ORM object passed to ``add``.
        """
        self.added.append(instance)

    def add_all(self, instances: Sequence[Any]) -> None:
        """
        Record multiple objects added to the session.

        Purpose:
            Verify bulk insert methods add every entity before flushing.

        Args:
            instances: ORM objects passed to ``add_all``.
        """
        self.added.extend(instances)

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


def make_palette_repository(session: RecordingSession) -> SqlAlchemyPaletteRepository:
    """Build a palette adapter bound to the recording session."""
    return SqlAlchemyPaletteRepository(cast(AsyncSession, session))


def make_account_repository(session: RecordingSession) -> SqlAlchemyAccountRepository:
    """Build an account adapter bound to the recording session."""
    return SqlAlchemyAccountRepository(cast(AsyncSession, session))


def make_project_repository(session: RecordingSession) -> SqlAlchemyProjectRepository:
    """Build a project adapter bound to the recording session."""
    return SqlAlchemyProjectRepository(cast(AsyncSession, session))


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
    repository = make_project_repository(session)

    await repository.list_projects(
        USER_ID,
        include_archived=False,
        limit=10,
        after_archived=None,
        after_name=None,
        after_project_id=None,
    )
    active_sql = compile_statement(session.statements[-1])

    await repository.list_projects(
        USER_ID,
        include_archived=True,
        limit=10,
        after_archived=None,
        after_name=None,
        after_project_id=None,
    )
    all_sql = compile_statement(session.statements[-1])

    ordering = (
        "ORDER BY CASE WHEN (projects.archived_at IS NULL) THEN 0 ELSE 1 END, "
        "lower(projects.name), projects.id"
    )
    assert "projects.user_id = 7" in active_sql
    assert "projects.deleted_at IS NULL" in active_sql
    assert "projects.archived_at IS NULL" in active_sql
    assert "lower(projects.name) AS name_key" in active_sql
    assert ordering in active_sql
    assert "LIMIT 10" in active_sql
    assert "projects.user_id = 7" in all_sql
    assert "projects.deleted_at IS NULL" in all_sql
    assert ordering in all_sql
    assert "LIMIT 10" in all_sql


async def test_list_projects_applies_archived_name_id_cursor_and_limit() -> None:
    """Project pagination applies the archive rank, name key, id tiebreaker, and limit."""
    session = RecordingSession()
    repository = make_project_repository(session)

    await repository.list_projects(
        USER_ID,
        include_archived=True,
        limit=6,
        after_archived=True,
        after_name="work",
        after_project_id=10,
    )
    sql = compile_statement(session.statements[-1])

    assert "CASE WHEN (projects.archived_at IS NULL) THEN 0 ELSE 1 END > 1" in sql
    assert "lower(projects.name) > 'work'" in sql
    assert "lower(projects.name) = 'work'" in sql
    assert "projects.id > 10" in sql
    assert (
        "ORDER BY CASE WHEN (projects.archived_at IS NULL) THEN 0 ELSE 1 END, "
        "lower(projects.name), projects.id"
    ) in sql
    assert "LIMIT 6" in sql


async def test_list_projects_returns_sql_lower_name_key() -> None:
    """Project listing maps rows to domain projects paired with the SQL key."""
    project = Project(id=1, user_id=USER_ID, name="Ärger", color_id=1)
    session = RecordingSession(results=[RecordingResult(values=[(project, "ärger")])])
    repository = make_project_repository(session)

    rows = await repository.list_projects(
        USER_ID,
        include_archived=False,
        limit=10,
        after_archived=None,
        after_name=None,
        after_project_id=None,
    )

    assert rows == [(project_to_entity(project), "ärger")]


async def test_project_repository_returns_domain_and_scopes_tenant() -> None:
    """Project reads return domain rows and always filter by user."""
    session = RecordingSession(results=[RecordingResult(values=[])])
    repository = make_project_repository(session)

    projects = await repository.list_projects(
        USER_ID,
        include_archived=False,
        limit=50,
        after_archived=None,
        after_name=None,
        after_project_id=None,
    )
    sql = compile_statement(session.statements[-1])

    assert projects == []
    assert "projects.user_id = 7" in sql
    assert "lower(projects.name) AS name_key" in sql


async def test_project_save_issues_update() -> None:
    """Saving a project applies the domain fields to the tracked row."""
    row = Project(id=PROJECT_ID, user_id=USER_ID, name="Old", color_id=1)
    session = RecordingSession(get_results=[row])
    repository = make_project_repository(session)
    project = ProjectEntity(
        id=PROJECT_ID,
        user_id=USER_ID,
        name="New",
        color_id=2,
        description="Updated",
    )

    await repository.save_project(project)

    assert row.name == "New"
    assert row.color_id == 2
    assert row.description == "Updated"
    assert session.flush_count == 1
    assert session.statements == []


async def test_project_add_inserts_fresh_row_without_domain_id() -> None:
    """New projects insert a fresh ORM row and never copy the transient id."""
    session = RecordingSession()
    repository = make_project_repository(session)
    project = ProjectEntity(user_id=USER_ID, name="Work", color_id=2)

    result = await repository.add_project(project)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.id is None
    assert row.user_id == USER_ID
    assert row.name == "Work"
    assert row.color_id == 2
    assert result.name == "Work"
    assert session.flush_count == 1


async def test_project_progress_for_projects_groups_by_project_id() -> None:
    """Batch progress counts non-deleted tasks grouped by project."""
    session = RecordingSession(results=[RecordingResult(values=[(5, 4, 2), (6, 1, 0)])])
    repository = make_project_repository(session)

    result = await repository.project_progress_for_projects(USER_ID, (5, 6))
    sql = compile_statement(session.statements[-1])

    assert result == {5: (4, 2), 6: (1, 0)}
    assert "count(tasks.id)" in sql
    assert "FILTER (WHERE tasks.completed_at IS NOT NULL)" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.project_id IN (5, 6)" in sql
    assert "GROUP BY tasks.project_id" in sql


async def test_get_palette_colors_by_ids_uses_in() -> None:
    """Batch palette loading selects every requested identifier in one query."""
    first = ColorPalette(
        id=1,
        slug="blue",
        name="Blue",
        hex="#0000FF",
        sort_order=1,
        is_active=True,
    )
    second = ColorPalette(
        id=3,
        slug="red",
        name="Red",
        hex="#FF0000",
        sort_order=3,
        is_active=False,
    )
    session = RecordingSession(results=[RecordingResult(values=[first, second])])
    repository = make_palette_repository(session)

    result = await repository.get_palette_colors_by_ids((1, 3))
    sql = compile_statement(session.statements[-1])

    assert result == {1: color_to_entity(first), 3: color_to_entity(second)}
    assert "color_palette.id IN (1, 3)" in sql


async def test_palette_repository_returns_domain_colors() -> None:
    """Palette reads map ORM rows to domain colors in display order."""
    row = ColorPalette(
        id=1,
        slug="blue",
        name="Blue",
        hex="#0000FF",
        sort_order=1,
        is_active=True,
    )
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_palette_repository(session)

    colors = await repository.list_palette_colors()
    sql = compile_statement(session.statements[-1])

    assert list(colors) == [color_to_entity(row)]
    assert isinstance(colors[0], ColorPaletteEntity)
    assert "ORDER BY color_palette.sort_order, color_palette.id" in sql


async def test_project_batch_methods_skip_empty_input() -> None:
    """Project batch methods return empty mappings without issuing queries."""
    session = RecordingSession()
    project_repository = make_project_repository(session)
    palette_repository = make_palette_repository(session)

    progress = await project_repository.project_progress_for_projects(USER_ID, ())
    colors = await palette_repository.get_palette_colors_by_ids(())

    assert progress == {}
    assert colors == {}
    assert session.statements == []


async def test_project_name_exists_is_case_insensitive_active_and_excludes_id() -> None:
    """Project name checks compare lower-case active names and skip the current id."""
    session = RecordingSession(
        results=[
            RecordingResult(scalar=1),
            RecordingResult(scalar=None),
        ]
    )
    repository = make_project_repository(session)

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
    project_repository = make_project_repository(session)
    label_repository = make_repository(session)

    await project_repository.get_project(USER_ID, PROJECT_ID)
    project_sql = compile_statement(session.statements[-1])

    await project_repository.get_project(USER_ID, PROJECT_ID, for_update=True)
    locked_project_sql = compile_statement(session.statements[-1])

    await label_repository.get_label(USER_ID, 9)
    label_sql = compile_statement(session.statements[-1])

    await label_repository.get_label(USER_ID, 9, for_update=True)
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
    repository = make_palette_repository(session)

    await repository.list_active_palette_colors()
    sql = compile_statement(session.statements[-1])

    assert "color_palette.is_active IS true" in sql
    assert "ORDER BY color_palette.sort_order, color_palette.id" in sql


async def test_get_default_palette_color_is_first_active() -> None:
    """The default palette color is the first active color by display order."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_palette_repository(session)

    await repository.get_default_palette_color()
    sql = compile_statement(session.statements[-1])

    assert "color_palette.is_active IS true" in sql
    assert "ORDER BY color_palette.sort_order, color_palette.id" in sql
    assert "LIMIT 1" in sql


async def test_soft_delete_project_emits_owner_scoped_cascade() -> None:
    """Project deletion updates the project tree and cancels active deliveries."""
    session = RecordingSession()
    repository = make_project_repository(session)
    project = ProjectEntity(id=PROJECT_ID, user_id=USER_ID, name="Work", color_id=1)

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
    repository = make_project_repository(session)

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
    repository = make_project_repository(session)

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
    repository = make_project_repository(session)

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
    repository = make_palette_repository(session)

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


async def test_list_due_reminders_filters_enabled_owners_and_active_projects() -> None:
    """Due reminder selection checks toggles, owner rows, and project state."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_due_reminders(NOW, limit=100)
    sql = compile_statement(session.statements[-1])

    assert "LIMIT 100" in sql
    assert "task_reminders.enabled IS true" in sql
    assert "task_reminders.next_run_at IS NOT NULL" in sql
    assert "task_reminders.next_run_at <=" in sql
    assert "tasks.user_id = task_reminders.user_id" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.completed_at IS NULL" in sql
    assert "tasks.reminders_enabled IS true" in sql
    assert "task_series.user_id = task_reminders.user_id" in sql
    assert "task_series.deleted_at IS NULL" in sql
    assert "task_series.reminders_enabled IS true" in sql
    assert "projects.deleted_at IS NULL" in sql
    assert "projects.archived_at IS NULL" in sql
    assert "ORDER BY task_reminders.next_run_at, task_reminders.id" in sql


async def test_claim_due_reminder_uses_due_checks_and_skip_locked() -> None:
    """Claiming a reminder verifies the enabled due slot without blocking."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_repository(session)

    result = await repository.claim_due_reminder(11, NOW)
    sql = compile_statement(session.statements[-1])

    assert result is None
    assert "task_reminders.id = 11" in sql
    assert "task_reminders.enabled IS true" in sql
    assert "task_reminders.next_run_at IS NOT NULL" in sql
    assert "task_reminders.next_run_at <=" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


async def test_list_series_for_generation_filters_horizon_and_active_projects() -> None:
    """Generation selection is non-deleted, below the horizon, and project-active."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.deleted_at IS NULL" in sql
    assert "task_series.generated_through IS NULL" in sql
    assert "task_series.generated_through <" in sql
    assert "task_series.project_id IS NULL" in sql
    assert "task_series.project_id IN (SELECT projects.id" in sql
    assert "projects.deleted_at IS NULL" in sql
    assert "projects.archived_at IS NULL" in sql
    assert "ORDER BY task_series.id" in sql


async def test_list_series_for_generation_excludes_ended_series() -> None:
    """Ended series are filtered by the one-day end-date lower bound."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.ends_on IS NULL" in sql
    assert "task_series.ends_on >= '2026-11-03'" in sql


async def test_list_series_for_generation_excludes_caught_up_series() -> None:
    """Caught-up series are filtered by the one-day horizon threshold."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.generated_through IS NULL" in sql
    assert "task_series.generated_through < '2027-02-01 12:00:00+00:00'" in sql


async def test_list_series_for_generation_includes_future_series_within_horizon() -> None:
    """Future starts are not constrained so the first occurrence can be created."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.starts_on >" not in sql
    assert "task_series.starts_on <" not in sql


async def test_list_series_for_generation_skips_far_future_series_after_first_generation() -> None:
    """A far-future generation marker exceeds the threshold exclusion bound."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    threshold = NOW + timedelta(days=90) - timedelta(days=1)
    assert f"task_series.generated_through < '{threshold.isoformat(sep=' ')}'" in sql


async def test_list_series_due_times_filters_future_slots() -> None:
    """Due-time lookup only loads slots at or after the current instant."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_series_due_times(3, USER_ID, now=NOW)
    sql = compile_statement(session.statements[-1])

    assert "tasks.series_id = 3" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.due_at >= '2026-11-04 12:00:00+00:00'" in sql


async def test_add_tasks_bulk_adds_and_flushes_once() -> None:
    """Bulk task insertion adds every task and flushes a single time."""
    session = RecordingSession()
    repository = make_repository(session)
    tasks = [
        make_task(due_at=NOW),
        make_task(due_at=NOW + timedelta(days=1)),
    ]

    result = await repository.add_tasks(tasks)

    assert list(result) == tasks
    assert session.added == tasks
    assert session.flush_count == 1


async def test_bulk_label_and_reminder_rows_add_all_and_flush_once() -> None:
    """Bulk label and reminder insertion adds every row and flushes once each."""
    session = RecordingSession()
    repository = make_repository(session)
    rows = [
        TaskLabel(task_id=1, label_id=2, user_id=USER_ID),
        TaskLabel(task_id=1, label_id=3, user_id=USER_ID),
    ]
    reminders = [
        TaskReminder(
            user_id=USER_ID,
            task_id=1,
            kind="before_due",
            enabled=True,
            offset_minutes=30,
        ),
        TaskReminder(
            user_id=USER_ID,
            task_id=2,
            kind="before_due",
            enabled=True,
            offset_minutes=30,
        ),
    ]

    await repository.add_task_label_rows(rows)
    await repository.add_reminders(reminders)

    assert session.added == rows + reminders
    assert session.flush_count == 2


async def test_bulk_add_methods_skip_empty_input() -> None:
    """Bulk insertion with no rows is a no-op without a flush."""
    session = RecordingSession()
    repository = make_repository(session)

    tasks = await repository.add_tasks([])
    await repository.add_task_label_rows([])
    await repository.add_reminders([])

    assert list(tasks) == []
    assert session.added == []
    assert session.flush_count == 0


async def test_pause_project_reminders_clears_slots_and_cancels_deliveries() -> None:
    """Pausing a project clears task and series reminder slots atomically."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.pause_project_reminders(USER_ID, PROJECT_ID)

    reminder_sql, delivery_sql = (compile_statement(statement) for statement in session.statements)
    assert len(session.statements) == 2
    assert session.flush_count == 1
    assert reminder_sql.startswith("UPDATE task_reminders SET next_run_at=NULL")
    assert "task_reminders.user_id = 7" in reminder_sql
    assert "task_reminders.task_id IN (SELECT tasks.id" in reminder_sql
    assert "task_reminders.series_id IN (SELECT task_series.id" in reminder_sql
    assert "tasks.project_id = 5" in reminder_sql
    assert "task_series.project_id = 5" in reminder_sql
    assert delivery_sql.startswith("UPDATE reminder_deliveries SET status='cancelled'")
    assert "reminder_deliveries.user_id = 7" in delivery_sql
    assert "reminder_deliveries.status IN ('pending', 'failed', 'sending')" in delivery_sql


async def test_resume_project_reminders_rebuilds_incomplete_and_overdue_tasks() -> None:
    """Resuming retargets templates and recomputes incomplete task slots."""
    from datetime import date, time, timedelta

    from backend.task_management.enums import ReminderKind
    from tests.task_management_fakes import make_reminder, make_series, make_task

    series = make_series(
        series_id=3,
        project_id=PROJECT_ID,
        time_of_day=time(9, 0),
        timezone="Asia/Tokyo",
        starts_on=date(2026, 11, 1),
    )
    template = make_reminder(
        reminder_id=1,
        series_id=series.id,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        interval_days=1,
        time_of_day=time(9, 0),
        timezone="UTC",
    )
    task = make_task(
        task_id=4,
        project_id=PROJECT_ID,
        due_at=NOW + timedelta(days=1),
        reminders_enabled=True,
    )
    task_reminder = make_reminder(
        reminder_id=2,
        task_id=task.id,
        kind=ReminderKind.BEFORE_DUE,
        offset_minutes=30,
    )
    session = RecordingSession(
        results=[
            RecordingResult(values=[series]),
            RecordingResult(values=[template]),
            RecordingResult(values=[task]),
            RecordingResult(values=[task_reminder]),
        ]
    )
    repository = make_repository(session)

    await repository.resume_project_reminders(USER_ID, PROJECT_ID, NOW)

    series_sql, template_sql, task_sql, task_reminder_sql = (
        compile_statement(statement) for statement in session.statements
    )
    assert "task_series.project_id = 5" in series_sql
    assert "task_series.user_id = 7" in series_sql
    assert "task_series.deleted_at IS NULL" in series_sql
    assert "task_reminders.series_id IN (3)" in template_sql
    assert "tasks.project_id = 5" in task_sql
    assert "tasks.deleted_at IS NULL" in task_sql
    assert "tasks.completed_at IS NULL" in task_sql
    assert "tasks.due_at >" not in task_sql
    assert "task_reminders.task_id IN (4)" in task_reminder_sql
    assert template.next_run_at is None
    assert template.timezone == "Asia/Tokyo"
    assert task_reminder.next_run_at == task.due_at - timedelta(minutes=30)


async def test_resume_project_reminders_batches_series_and_task_reminders() -> None:
    """Unarchiving loads series and task reminders in two batched statements."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.resume_project_reminders(USER_ID, PROJECT_ID, NOW)

    statements = "\n".join(str(statement) for statement in session.statements)
    reminder_sql = [
        compile_statement(statement)
        for statement in session.statements
        if "FROM task_reminders" in str(statement)
    ]
    assert statements.count("FROM task_reminders") == 2
    assert len(reminder_sql) == 2
    assert all("task_reminders.user_id = 7" in sql for sql in reminder_sql)
    assert "IN (" in statements
    assert "task_reminders.series_id IN" in statements
    assert "task_reminders.task_id IN" in statements


async def test_get_telegram_id_reads_the_linked_profile() -> None:
    """Reminder targeting resolves the Telegram profile of the internal user."""
    session = RecordingSession(results=[RecordingResult(scalar=99)])
    repository = make_account_repository(session)

    telegram_id = await repository.get_telegram_id(USER_ID)
    sql = compile_statement(session.statements[-1])

    assert telegram_id == 99
    assert "telegram_profiles.telegram_id" in sql
    assert "telegram_profiles.user_id = 7" in sql


async def test_account_repository_reads_timezone_and_telegram_id() -> None:
    """Account reads return the stored timezone and Telegram chat identifier."""
    session = RecordingSession(
        results=[
            RecordingResult(scalar="Asia/Tokyo"),
            RecordingResult(scalar=99),
        ]
    )
    repository = make_account_repository(session)

    timezone = await repository.get_user_timezone(USER_ID)
    telegram_id = await repository.get_telegram_id(USER_ID)

    timezone_sql = compile_statement(session.statements[0])
    telegram_sql = compile_statement(session.statements[1])
    assert timezone == "Asia/Tokyo"
    assert telegram_id == 99
    assert "users.timezone" in timezone_sql
    assert "users.id = 7" in timezone_sql
    assert "telegram_profiles.telegram_id" in telegram_sql
    assert "telegram_profiles.user_id = 7" in telegram_sql


async def test_resume_project_reminders_clears_slot_on_datetime_overflow() -> None:
    """An extreme stored reminder is cleared instead of failing unarchive."""
    from datetime import UTC

    from backend.task_management.enums import ReminderKind
    from tests.task_management_fakes import make_reminder, make_task

    task = make_task(
        task_id=4,
        project_id=PROJECT_ID,
        due_at=datetime.min.replace(tzinfo=UTC),
        reminders_enabled=True,
    )
    reminder = make_reminder(
        reminder_id=2,
        task_id=task.id,
        kind=ReminderKind.BEFORE_DUE,
        offset_minutes=1,
    )
    session = RecordingSession(
        results=[
            RecordingResult(values=[]),
            RecordingResult(values=[]),
            RecordingResult(values=[task]),
            RecordingResult(values=[reminder]),
        ]
    )
    repository = make_repository(session)

    await repository.resume_project_reminders(USER_ID, PROJECT_ID, NOW)

    assert reminder.next_run_at is None


async def test_list_tasks_applies_cursor_and_limit() -> None:
    """Task pagination applies the compound cursor predicate and a bounded limit."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_tasks(
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
        limit=26,
        after_due_at=NOW,
        after_task_id=10,
    )
    sql = compile_statement(session.statements[-1])

    assert "tasks.due_at > '" in sql
    assert "tasks.due_at = '" in sql
    assert "tasks.id > 10" in sql
    assert "ORDER BY tasks.due_at, tasks.id" in sql
    assert "LIMIT 26" in sql


async def test_list_tasks_batch_methods_filter_by_user_and_ids() -> None:
    """Batch enrichment scopes each query to the owner and the requested ids."""
    session = RecordingSession()
    repository = make_repository(session)

    labels = await repository.list_task_labels_for_tasks(USER_ID, (1, 2))
    labels_sql = compile_statement(session.statements[-1])

    reminders = await repository.list_task_reminders_for_tasks(USER_ID, (1, 2))
    reminders_sql = compile_statement(session.statements[-1])

    projects = await repository.get_projects_by_ids(USER_ID, (5, 6))
    projects_sql = compile_statement(session.statements[-1])

    assert labels == {}
    assert reminders == {}
    assert projects == {}
    assert "task_labels.user_id = 7" in labels_sql
    assert "task_labels.task_id IN (1, 2)" in labels_sql
    assert "labels.user_id = 7" in labels_sql
    assert "labels.deleted_at IS NULL" in labels_sql
    assert "task_reminders.user_id = 7" in reminders_sql
    assert "task_reminders.task_id IN (1, 2)" in reminders_sql
    assert "projects.user_id = 7" in projects_sql
    assert "projects.deleted_at IS NULL" in projects_sql
    assert "projects.id IN (5, 6)" in projects_sql


async def test_get_task_and_series_are_tenant_scoped_and_lockable() -> None:
    """Task and series lookups filter by owner and lock rows on demand."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.get_task(USER_ID, 11)
    task_sql = compile_statement(session.statements[-1])

    await repository.get_task(USER_ID, 11, for_update=True)
    locked_task_sql = compile_statement(session.statements[-1])

    await repository.get_series(USER_ID, 3)
    series_sql = compile_statement(session.statements[-1])

    await repository.get_series(USER_ID, 3, for_update=True)
    locked_series_sql = compile_statement(session.statements[-1])

    assert "tasks.id = 11" in task_sql
    assert "tasks.user_id = 7" in task_sql
    assert "tasks.deleted_at IS NULL" in task_sql
    assert "FOR UPDATE" not in task_sql
    assert "FOR UPDATE" in locked_task_sql
    assert "task_series.id = 3" in series_sql
    assert "task_series.user_id = 7" in series_sql
    assert "task_series.deleted_at IS NULL" in series_sql
    assert "FOR UPDATE" not in series_sql
    assert "FOR UPDATE" in locked_series_sql


async def test_list_tasks_and_series_are_tenant_scoped() -> None:
    """Task and series listings always filter by the owning user."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.list_tasks(
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
        limit=50,
        after_due_at=None,
        after_task_id=None,
    )
    tasks_sql = compile_statement(session.statements[-1])

    await repository.list_series(USER_ID)
    series_sql = compile_statement(session.statements[-1])

    assert "tasks.user_id = 7" in tasks_sql
    assert "tasks.deleted_at IS NULL" in tasks_sql
    assert "LIMIT 50" in tasks_sql
    assert "task_series.user_id = 7" in series_sql
    assert "task_series.deleted_at IS NULL" in series_sql


async def test_soft_delete_task_marks_only_the_scoped_task() -> None:
    """Soft deletion mutates only the already owner-scoped task instance."""
    session = RecordingSession()
    repository = make_repository(session)
    task = make_task(task_id=4, user_id=USER_ID, due_at=NOW)
    other = make_task(task_id=5, user_id=USER_ID, due_at=NOW)

    await repository.soft_delete_task(task, NOW, BATCH_ID)

    assert task.deleted_at == NOW
    assert task.deletion_batch_id == BATCH_ID
    assert other.deleted_at is None
    assert session.flush_count == 1


async def test_add_task_labels_stamps_owner_on_every_row() -> None:
    """Task label assignments persist the owner on every inserted row."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.add_task_labels(11, USER_ID, (1, 2))

    assert [(row.task_id, row.label_id, row.user_id) for row in session.added] == [
        (11, 1, USER_ID),
        (11, 2, USER_ID),
    ]
    assert session.flush_count == 1


async def test_get_labels_by_ids_filters_by_owner_and_ids() -> None:
    """Batch label lookup is tenant-scoped, non-deleted, and bounded by IN."""
    session = RecordingSession()
    repository = make_repository(session)

    result = await repository.get_labels_by_ids(USER_ID, (1, 2))
    sql = compile_statement(session.statements[-1])

    assert list(result) == []
    assert "labels.user_id = 7" in sql
    assert "labels.deleted_at IS NULL" in sql
    assert "labels.id IN (1, 2)" in sql


async def test_list_tasks_regular_joins_daily_series_without_deleted_filter() -> None:
    """Regular recurrence joins the series table and filters daily series only."""
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
    """Periodic recurrence joins the series table and filters periodic series only."""
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
    """Series lookup skips the deleted filter when deleted rows are requested."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_repository(session)
    await repository.get_series(USER_ID, 3, include_deleted=True)
    sql = compile_statement(session.statements[-1])
    assert "task_series.id = 3" in sql
    assert "task_series.user_id = 7" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_get_series_by_ids_is_tenant_scoped_and_optional_deleted() -> None:
    """Batch series lookup is tenant-scoped and can include soft-deleted rows."""
    session = RecordingSession()
    repository = make_repository(session)
    await repository.get_series_by_ids(USER_ID, (1, 2), include_deleted=True)
    sql = compile_statement(session.statements[-1])
    assert "task_series.user_id = 7" in sql
    assert "task_series.id IN (1, 2)" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_hard_delete_tasks_is_user_scoped_and_physical() -> None:
    """Hard deletion issues one owner-scoped DELETE for the requested tasks."""
    session = RecordingSession()
    repository = make_repository(session)
    await repository.hard_delete_tasks((4, 5), USER_ID)
    sql = compile_statement(session.statements[-1])
    assert sql.startswith("DELETE FROM tasks")
    assert "tasks.id IN (4, 5)" in sql
    assert "tasks.user_id = 7" in sql


async def test_series_due_slot_exists_is_owner_scoped_and_excludes_task() -> None:
    """The slot lookup is owner-scoped and ignores the edited task."""
    session = RecordingSession(results=[RecordingResult(scalar=1)])
    repository = make_repository(session)

    exists = await repository.series_due_slot_exists(
        3,
        USER_ID,
        NOW,
        exclude_task_id=9,
    )

    assert exists is True
    sql = compile_statement(session.statements[-1])
    assert "tasks.series_id = 3" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.due_at = " in sql
    assert "tasks.id != 9" in sql
    assert "LIMIT 1" in sql
