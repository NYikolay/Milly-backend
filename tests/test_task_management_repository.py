from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import ColorPalette as ColorPaletteEntity
from backend.domain.entities import Label as LabelEntity
from backend.domain.entities import Project as ProjectEntity
from backend.domain.entities import ReminderDelivery as ReminderDeliveryEntity
from backend.domain.entities import Task as TaskEntity
from backend.domain.entities import TaskLabel as TaskLabelEntity
from backend.domain.entities import TaskReminder as TaskReminderEntity
from backend.domain.entities import TaskSeries as TaskSeriesEntity
from backend.domain.enums import DeliveryStatus
from backend.infrastructure.persistence.sqlalchemy.account_repository import (
    SqlAlchemyAccountRepository,
)
from backend.infrastructure.persistence.sqlalchemy.label_repository import (
    SqlAlchemyLabelRepository,
)
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    color_to_entity,
    delivery_to_entity,
    label_to_entity,
    project_to_entity,
    reminder_to_entity,
    series_to_entity,
    task_to_entity,
)
from backend.infrastructure.persistence.sqlalchemy.palette_repository import (
    SqlAlchemyPaletteRepository,
)
from backend.infrastructure.persistence.sqlalchemy.project_repository import (
    SqlAlchemyProjectRepository,
)
from backend.infrastructure.persistence.sqlalchemy.reminder_repository import (
    SqlAlchemyReminderRepository,
)
from backend.infrastructure.persistence.sqlalchemy.series_repository import (
    SqlAlchemySeriesRepository,
)
from backend.infrastructure.persistence.sqlalchemy.task_repository import (
    SqlAlchemyTaskRepository,
)
from backend.models.color_palette import ColorPalette
from backend.models.project import Project
from tests.task_management_fakes import (
    make_delivery,
    make_label,
    make_reminder,
    make_series,
    make_task,
)

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
        assigned_ids: Sequence[int] | None = None,
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
            assigned_ids: Identifiers assigned to newly added rows during
                flushes, simulating PostgreSQL identity columns.
        """
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flush_count = 0
        self.get_count = 0
        self._results = list(results or [])
        self._get_results = list(get_results or [])
        self._assigned_ids = list(assigned_ids or [])

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
        self.get_count += 1
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
        Record a flush and assign identities to new rows.

        Purpose:
            Verify write methods flush without committing and simulate the
            identifiers PostgreSQL generates for inserted rows.

        Returns:
            None: No database work is performed.
        """
        self.flush_count += 1
        for instance in self.added:
            if getattr(instance, "id", None) is None and self._assigned_ids:
                instance.id = self._assigned_ids.pop(0)


def make_palette_repository(session: RecordingSession) -> SqlAlchemyPaletteRepository:
    """Build a palette adapter bound to the recording session."""
    return SqlAlchemyPaletteRepository(cast(AsyncSession, session))


def make_account_repository(session: RecordingSession) -> SqlAlchemyAccountRepository:
    """Build an account adapter bound to the recording session."""
    return SqlAlchemyAccountRepository(cast(AsyncSession, session))


def make_project_repository(session: RecordingSession) -> SqlAlchemyProjectRepository:
    """Build a project adapter bound to the recording session."""
    return SqlAlchemyProjectRepository(cast(AsyncSession, session))


def make_label_repository(session: RecordingSession) -> SqlAlchemyLabelRepository:
    """Build a label adapter bound to the recording session."""
    return SqlAlchemyLabelRepository(cast(AsyncSession, session))


def make_task_repository(session: RecordingSession) -> SqlAlchemyTaskRepository:
    """Build a task adapter bound to the recording session."""
    return SqlAlchemyTaskRepository(cast(AsyncSession, session))


def make_series_repository(session: RecordingSession) -> SqlAlchemySeriesRepository:
    """Build a series adapter bound to the recording session."""
    return SqlAlchemySeriesRepository(cast(AsyncSession, session))


def make_reminder_repository(session: RecordingSession) -> SqlAlchemyReminderRepository:
    """Build a reminder adapter bound to the recording session."""
    return SqlAlchemyReminderRepository(cast(AsyncSession, session))


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
    repository = make_label_repository(session)

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


async def test_label_repository_lists_domain_labels_in_display_order() -> None:
    """Label listing maps ORM rows to domain labels ordered by name and id."""
    first = make_label(label_id=2, name="Home")
    second = make_label(label_id=3, name="Work")
    session = RecordingSession(results=[RecordingResult(values=[first, second])])
    repository = make_label_repository(session)

    labels = await repository.list_labels(USER_ID)
    sql = compile_statement(session.statements[-1])

    assert list(labels) == [label_to_entity(first), label_to_entity(second)]
    assert isinstance(labels[0], LabelEntity)
    assert "labels.user_id = 7" in sql
    assert "labels.deleted_at IS NULL" in sql
    assert "ORDER BY lower(labels.name), labels.id" in sql


async def test_label_repository_returns_domain_label() -> None:
    """Label lookup maps the ORM row to a domain label."""
    row = make_label(label_id=9, name="Home")
    session = RecordingSession(results=[RecordingResult(scalar=row)])
    repository = make_label_repository(session)

    label = await repository.get_label(USER_ID, 9)

    assert label == label_to_entity(row)


async def test_label_add_inserts_fresh_row_without_domain_id() -> None:
    """New labels insert a fresh ORM row and return a domain entity."""
    session = RecordingSession()
    repository = make_label_repository(session)
    label = LabelEntity(user_id=USER_ID, name="Home", color_id=2)

    result = await repository.add_label(label)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.id is None
    assert row.user_id == USER_ID
    assert row.name == "Home"
    assert row.color_id == 2
    assert isinstance(result, LabelEntity)
    assert result.name == "Home"
    assert session.flush_count == 1


async def test_label_save_issues_update() -> None:
    """Saving a label applies the domain fields to the tracked row."""
    row = make_label(label_id=9, name="Old")
    session = RecordingSession(get_results=[row])
    repository = make_label_repository(session)
    label = LabelEntity(id=9, user_id=USER_ID, name="New", color_id=3)

    await repository.save_label(label)

    assert row.name == "New"
    assert row.color_id == 3
    assert session.flush_count == 1
    assert session.statements == []


async def test_label_save_transient_delegates_to_add() -> None:
    """Saving a transient label inserts it instead of updating."""
    session = RecordingSession()
    repository = make_label_repository(session)
    label = LabelEntity(user_id=USER_ID, name="Home", color_id=1)

    await repository.save_label(label)

    assert len(session.added) == 1
    assert session.added[0].id is None
    assert session.flush_count == 1


async def test_label_soft_delete_issues_scoped_update() -> None:
    """Soft-deleting a label marks the domain entity and updates only its row."""
    session = RecordingSession()
    repository = make_label_repository(session)
    label = LabelEntity(id=9, user_id=USER_ID, name="Home", color_id=1)

    await repository.soft_delete_label(label, NOW)
    sql = compile_statement(session.statements[-1])

    assert label.deleted_at == NOW
    assert sql.startswith("UPDATE labels SET deleted_at=")
    assert "labels.id = 9" in sql
    assert session.flush_count == 1


async def test_get_project_and_label_are_tenant_scoped_and_can_lock() -> None:
    """Project and label lookups filter by owner, exclude deleted rows, and lock on demand."""
    session = RecordingSession()
    project_repository = make_project_repository(session)
    label_repository = make_label_repository(session)

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


async def test_series_repository_claim_uses_skip_locked() -> None:
    """Series generation claims remain concurrency-safe."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_series_repository(session)

    result = await repository.claim_series_for_generation(3, USER_ID)
    sql = compile_statement(session.statements[-1])

    assert result is None
    assert "task_series.id = 3" in sql
    assert "task_series.user_id = 7" in sql
    assert "task_series.deleted_at IS NULL" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert session.flush_count == 0


async def test_list_due_reminders_filters_enabled_owners_and_active_projects() -> None:
    """Due reminder selection checks toggles, owner rows, and project state."""
    row = make_reminder(reminder_id=1, task_id=11, next_run_at=NOW)
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_reminder_repository(session)

    reminders = await repository.list_due_reminders(NOW, limit=100)
    sql = compile_statement(session.statements[-1])

    assert list(reminders) == [reminder_to_entity(row)]
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


async def test_reminder_repository_claim_due_reminder_uses_lock() -> None:
    """Claiming a reminder verifies the enabled due slot without blocking."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_reminder_repository(session)

    result = await repository.claim_due_reminder(11, NOW)
    sql = compile_statement(session.statements[-1])

    assert result is None
    assert "task_reminders.id = 11" in sql
    assert "task_reminders.enabled IS true" in sql
    assert "task_reminders.next_run_at IS NOT NULL" in sql
    assert "task_reminders.next_run_at <=" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert session.flush_count == 0


async def test_list_series_for_generation_filters_horizon_and_active_projects() -> None:
    """Generation selection is non-deleted, below the horizon, and project-active."""
    session = RecordingSession()
    repository = make_series_repository(session)

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
    repository = make_series_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.ends_on IS NULL" in sql
    assert "task_series.ends_on >= '2026-11-03'" in sql


async def test_list_series_for_generation_excludes_caught_up_series() -> None:
    """Caught-up series are filtered by the one-day horizon threshold."""
    session = RecordingSession()
    repository = make_series_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.generated_through IS NULL" in sql
    assert "task_series.generated_through < '2027-02-01 12:00:00+00:00'" in sql


async def test_list_series_for_generation_includes_future_series_within_horizon() -> None:
    """Future starts are not constrained so the first occurrence can be created."""
    session = RecordingSession()
    repository = make_series_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    assert "task_series.starts_on >" not in sql
    assert "task_series.starts_on <" not in sql


async def test_list_series_for_generation_skips_far_future_series_after_first_generation() -> None:
    """A far-future generation marker exceeds the threshold exclusion bound."""
    session = RecordingSession()
    repository = make_series_repository(session)

    await repository.list_series_for_generation(NOW)
    sql = compile_statement(session.statements[-1])

    threshold = NOW + timedelta(days=90) - timedelta(days=1)
    assert f"task_series.generated_through < '{threshold.isoformat(sep=' ')}'" in sql


async def test_list_series_due_times_filters_future_slots() -> None:
    """Due-time lookup only loads slots at or after the current instant."""
    session = RecordingSession()
    repository = make_series_repository(session)

    await repository.list_series_due_times(3, USER_ID, now=NOW)
    sql = compile_statement(session.statements[-1])

    assert "tasks.series_id = 3" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.due_at >= '2026-11-04 12:00:00+00:00'" in sql


async def test_add_tasks_bulk_adds_and_flushes_once() -> None:
    """Bulk task insertion builds fresh rows, maps domains, and flushes once."""
    session = RecordingSession()
    repository = make_task_repository(session)
    tasks = [
        TaskEntity(user_id=USER_ID, title="First", due_at=NOW),
        TaskEntity(user_id=USER_ID, title="Second", due_at=NOW + timedelta(days=1)),
    ]

    result = await repository.add_tasks(tasks)

    assert [task.title for task in result] == ["First", "Second"]
    assert all(isinstance(task, TaskEntity) for task in result)
    assert [row.title for row in session.added] == ["First", "Second"]
    assert all(row.id is None for row in session.added)
    assert session.flush_count == 1


async def test_bulk_task_label_rows_adds_orm_rows_and_flushes_once() -> None:
    """Bulk task-label insertion maps domain rows and flushes once."""
    session = RecordingSession()
    repository = make_task_repository(session)
    rows = [
        TaskLabelEntity(task_id=1, label_id=2, user_id=USER_ID),
        TaskLabelEntity(task_id=1, label_id=3, user_id=USER_ID),
    ]

    await repository.add_task_label_rows(rows)

    assert [(row.task_id, row.label_id, row.user_id) for row in session.added] == [
        (1, 2, USER_ID),
        (1, 3, USER_ID),
    ]
    assert session.flush_count == 1


async def test_bulk_reminder_rows_add_all_and_flush_once() -> None:
    """Bulk reminder insertion maps domain rows and flushes once."""
    session = RecordingSession(assigned_ids=[11, 12])
    repository = make_reminder_repository(session)
    reminders = [
        TaskReminderEntity(
            user_id=USER_ID,
            task_id=1,
            kind="before_due",
            enabled=True,
            offset_minutes=30,
        ),
        TaskReminderEntity(
            user_id=USER_ID,
            task_id=2,
            kind="before_due",
            enabled=True,
            offset_minutes=30,
        ),
    ]

    await repository.add_reminders(reminders)

    assert [(row.task_id, row.kind, row.user_id) for row in session.added] == [
        (1, "before_due", USER_ID),
        (2, "before_due", USER_ID),
    ]
    assert [row.id for row in session.added] == [11, 12]
    assert [reminder.id for reminder in reminders] == [11, 12]
    assert session.flush_count == 1


async def test_bulk_add_methods_skip_empty_input() -> None:
    """Bulk insertion with no rows is a no-op without a flush."""
    session = RecordingSession()
    task_repository = make_task_repository(session)
    repository = make_reminder_repository(session)

    tasks = await task_repository.add_tasks([])
    await task_repository.add_task_label_rows([])
    await repository.add_reminders([])

    assert list(tasks) == []
    assert session.added == []
    assert session.flush_count == 0


async def test_pause_project_reminders_clears_slots_and_cancels_deliveries() -> None:
    """Pausing a project clears task and series reminder slots atomically."""
    session = RecordingSession()
    repository = make_reminder_repository(session)

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


async def test_list_project_reminder_contexts_reads_series_and_task_contexts() -> None:
    """Context reads return mapped reminders without mutating or flushing."""
    from backend.domain.enums import ReminderKind

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
        next_run_at=NOW,
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
        next_run_at=NOW,
    )
    session = RecordingSession(
        results=[
            RecordingResult(values=[series]),
            RecordingResult(values=[template]),
            RecordingResult(values=[task]),
            RecordingResult(values=[task_reminder]),
        ]
    )
    repository = make_reminder_repository(session)

    contexts = await repository.list_project_reminder_contexts(USER_ID, PROJECT_ID)

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
    assert [(context.is_series, context.series_timezone) for context in contexts] == [
        (True, "Asia/Tokyo"),
        (False, None),
    ]
    assert [context.task_due_at for context in contexts] == [None, task.due_at]
    assert [context.reminder.id for context in contexts] == [template.id, task_reminder.id]
    assert template.next_run_at == NOW
    assert template.timezone == "UTC"
    assert task_reminder.next_run_at == NOW
    assert session.flush_count == 0


async def test_list_project_reminder_contexts_batches_series_and_task_reminders() -> None:
    """Project resume contexts load series and task reminders in two batched statements."""
    session = RecordingSession()
    repository = make_reminder_repository(session)

    contexts = await repository.list_project_reminder_contexts(USER_ID, PROJECT_ID)

    statements = "\n".join(str(statement) for statement in session.statements)
    reminder_sql = [
        compile_statement(statement)
        for statement in session.statements
        if "FROM task_reminders" in str(statement)
    ]
    assert contexts == []
    assert statements.count("FROM task_reminders") == 2
    assert len(reminder_sql) == 2
    assert all("task_reminders.user_id = 7" in sql for sql in reminder_sql)
    assert "IN (" in statements
    assert "task_reminders.series_id IN" in statements
    assert "task_reminders.task_id IN" in statements
    assert session.flush_count == 0


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


async def test_save_reminders_applies_all_changes_with_one_batched_read() -> None:
    """Batch saves resolve rows with one owner-scoped select and flush once."""
    first_row = make_reminder(reminder_id=2, task_id=4)
    second_row = make_reminder(reminder_id=3, user_id=8, task_id=9)
    session = RecordingSession(results=[RecordingResult(values=[first_row, second_row])])
    repository = make_reminder_repository(session)
    first = TaskReminderEntity(
        id=2,
        user_id=USER_ID,
        task_id=4,
        kind="before_due",
        offset_minutes=15,
        next_run_at=NOW,
    )
    second = TaskReminderEntity(
        id=3,
        user_id=8,
        task_id=9,
        kind="scheduled",
        interval_days=2,
        time_of_day=time(8, 0),
        timezone="Asia/Tokyo",
        next_run_at=None,
    )

    await repository.save_reminders([first, second])

    assert first_row.offset_minutes == 15
    assert first_row.next_run_at == NOW
    assert second_row.interval_days == 2
    assert second_row.time_of_day == time(8, 0)
    assert second_row.timezone == "Asia/Tokyo"
    assert second_row.next_run_at is None
    assert len(session.statements) == 1
    sql = compile_statement(session.statements[0])
    assert sql.startswith("SELECT task_reminders")
    assert "task_reminders.id IN (2, 3)" in sql
    assert "task_reminders.user_id IN (7, 8)" in sql
    assert session.get_count == 0
    assert session.flush_count == 1


async def test_save_reminders_raises_for_a_missing_row_without_flushing() -> None:
    """Batch saves fail when one requested reminder row is not returned."""
    first_row = make_reminder(reminder_id=2, task_id=4)
    session = RecordingSession(results=[RecordingResult(values=[first_row])])
    repository = make_reminder_repository(session)
    first = TaskReminderEntity(id=2, user_id=USER_ID, task_id=4, kind="before_due")
    missing = TaskReminderEntity(id=3, user_id=USER_ID, task_id=5, kind="before_due")

    with pytest.raises(LookupError):
        await repository.save_reminders([first, missing])

    assert len(session.statements) == 1
    assert session.get_count == 0
    assert session.flush_count == 0


async def test_save_reminders_flushes_an_empty_batch_once() -> None:
    """An empty project resume batch keeps the previous single flush boundary."""
    session = RecordingSession()
    repository = make_reminder_repository(session)

    await repository.save_reminders([])

    assert session.statements == []
    assert session.flush_count == 1


async def test_task_repository_list_keeps_cursor_and_tenant_scope() -> None:
    """Task listing preserves ordering, limit, and user scope."""
    session = RecordingSession(results=[RecordingResult(values=[])])
    repository = make_task_repository(session)
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
    assert "tasks.due_at > '" in sql
    assert "tasks.due_at = '" in sql
    assert "tasks.id > 9" in sql
    assert "ORDER BY tasks.due_at, tasks.id" in sql
    assert "LIMIT 51" in sql


async def test_task_repository_list_maps_domain_tasks() -> None:
    """Task listing maps ORM rows to domain tasks."""
    row = make_task(task_id=11, due_at=NOW)
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_task_repository(session)

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
        limit=50,
        after_due_at=None,
        after_task_id=None,
    )

    assert list(tasks) == [task_to_entity(row)]
    assert isinstance(tasks[0], TaskEntity)


async def test_task_add_inserts_fresh_row_without_domain_id() -> None:
    """New tasks insert a fresh ORM row and return a domain entity."""
    session = RecordingSession()
    repository = make_task_repository(session)
    task = TaskEntity(user_id=USER_ID, title="New", due_at=NOW)

    result = await repository.add_task(task)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.id is None
    assert row.user_id == USER_ID
    assert row.title == "New"
    assert row.due_at == NOW
    assert isinstance(result, TaskEntity)
    assert result.title == "New"
    assert session.flush_count == 1


async def test_task_save_issues_update() -> None:
    """Saving a task applies the domain fields to the tracked row."""
    row = make_task(task_id=11, title="Old", due_at=NOW)
    session = RecordingSession(get_results=[row])
    repository = make_task_repository(session)
    task = TaskEntity(
        id=11,
        user_id=USER_ID,
        title="New",
        description="Updated",
        due_at=NOW + timedelta(hours=1),
        completed_at=NOW,
    )

    await repository.save_task(task)

    assert row.title == "New"
    assert row.description == "Updated"
    assert row.due_at == NOW + timedelta(hours=1)
    assert row.completed_at == NOW
    assert session.flush_count == 1
    assert session.statements == []


async def test_task_save_transient_delegates_to_add() -> None:
    """Saving a transient task inserts it instead of updating."""
    session = RecordingSession()
    repository = make_task_repository(session)
    task = TaskEntity(user_id=USER_ID, title="New", due_at=NOW)

    await repository.save_task(task)

    assert len(session.added) == 1
    assert session.added[0].id is None
    assert session.flush_count == 1


async def test_list_tasks_batch_methods_filter_by_user_and_ids() -> None:
    """Batch enrichment scopes each query to the owner and the requested ids."""
    session = RecordingSession()
    task_repository = make_task_repository(session)
    project_repository = make_project_repository(session)

    labels = await task_repository.list_task_labels_for_tasks(USER_ID, (1, 2))
    labels_sql = compile_statement(session.statements[-1])

    reminders = await task_repository.list_task_reminders_for_tasks(USER_ID, (1, 2))
    reminders_sql = compile_statement(session.statements[-1])

    projects = await project_repository.get_projects_by_ids(USER_ID, (5, 6))
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


async def test_batch_task_loaders_map_domain_rows() -> None:
    """Batch task loaders group mapped domain labels and reminders."""
    first_label = make_label(label_id=2)
    second_label = make_label(label_id=3)
    reminder = make_reminder(reminder_id=4, task_id=1)
    session = RecordingSession(
        results=[
            RecordingResult(values=[(first_label, 1), (second_label, 1)]),
            RecordingResult(values=[reminder]),
        ]
    )
    repository = make_task_repository(session)

    labels = await repository.list_task_labels_for_tasks(USER_ID, (1, 2))
    reminders = await repository.list_task_reminders_for_tasks(USER_ID, (1, 2))

    assert labels == {
        1: (label_to_entity(first_label), label_to_entity(second_label)),
    }
    assert reminders == {1: (reminder_to_entity(reminder),)}


async def test_get_task_and_series_are_tenant_scoped_and_lockable() -> None:
    """Task and series lookups filter by owner and lock rows on demand."""
    session = RecordingSession()
    task_repository = make_task_repository(session)
    repository = make_series_repository(session)

    await task_repository.get_task(USER_ID, 11)
    task_sql = compile_statement(session.statements[-1])

    await task_repository.get_task(USER_ID, 11, for_update=True)
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


async def test_task_repository_returns_domain_task() -> None:
    """Task lookup maps the ORM row to a domain task."""
    row = make_task(task_id=11, due_at=NOW)
    session = RecordingSession(results=[RecordingResult(scalar=row)])
    repository = make_task_repository(session)

    task = await repository.get_task(USER_ID, 11)

    assert task == task_to_entity(row)


async def test_list_tasks_and_series_are_tenant_scoped() -> None:
    """Task and series listings always filter by the owning user."""
    session = RecordingSession()
    task_repository = make_task_repository(session)
    repository = make_series_repository(session)

    await task_repository.list_tasks(
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
    """Soft deletion updates only the already owner-scoped task row."""
    session = RecordingSession()
    repository = make_task_repository(session)
    task = TaskEntity(id=4, user_id=USER_ID, title="Task", due_at=NOW)
    other = TaskEntity(id=5, user_id=USER_ID, title="Other", due_at=NOW)

    await repository.soft_delete_task(task, NOW, BATCH_ID)
    sql = compile_statement(session.statements[-1])

    assert task.deleted_at == NOW
    assert task.deletion_batch_id == BATCH_ID
    assert other.deleted_at is None
    assert sql.startswith("UPDATE tasks SET deleted_at=")
    assert "deletion_batch_id=" in sql
    assert "tasks.id = 4" in sql
    assert session.flush_count == 1


async def test_add_task_labels_stamps_owner_on_every_row() -> None:
    """Task label assignments persist the owner on every inserted row."""
    session = RecordingSession()
    repository = make_task_repository(session)

    await repository.add_task_labels(11, USER_ID, (1, 2))

    assert [(row.task_id, row.label_id, row.user_id) for row in session.added] == [
        (11, 1, USER_ID),
        (11, 2, USER_ID),
    ]
    assert session.flush_count == 1


async def test_replace_task_labels_deletes_then_reassigns() -> None:
    """Replacing task labels deletes the old selection and inserts the new one."""
    session = RecordingSession()
    repository = make_task_repository(session)

    await repository.replace_task_labels(11, USER_ID, (1, 2))

    delete_sql = compile_statement(session.statements[0])
    assert delete_sql.startswith("DELETE FROM task_labels")
    assert "task_labels.task_id = 11" in delete_sql
    assert "task_labels.user_id = 7" in delete_sql
    assert [(row.task_id, row.label_id) for row in session.added] == [(11, 1), (11, 2)]
    assert session.flush_count == 1


async def test_get_labels_by_ids_filters_by_owner_and_ids() -> None:
    """Batch label lookup is tenant-scoped, non-deleted, and bounded by IN."""
    row = make_label(label_id=1)
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_label_repository(session)

    result = await repository.get_labels_by_ids(USER_ID, (1, 2))
    sql = compile_statement(session.statements[-1])

    assert list(result) == [label_to_entity(row)]
    assert "labels.user_id = 7" in sql
    assert "labels.deleted_at IS NULL" in sql
    assert "labels.id IN (1, 2)" in sql


async def test_list_task_labels_returns_domain_labels() -> None:
    """Task label listing joins assignments and maps domain labels."""
    row = make_label(label_id=2)
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_task_repository(session)

    result = await repository.list_task_labels(USER_ID, 11)
    sql = compile_statement(session.statements[-1])

    assert list(result) == [label_to_entity(row)]
    assert "task_labels.task_id = 11" in sql
    assert "task_labels.user_id = 7" in sql
    assert "labels.user_id = 7" in sql
    assert "labels.deleted_at IS NULL" in sql
    assert "ORDER BY labels.id" in sql


async def test_list_tasks_regular_joins_daily_series_without_deleted_filter() -> None:
    """Regular recurrence joins the series table and filters daily series only."""
    session = RecordingSession()
    repository = make_task_repository(session)
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
    repository = make_task_repository(session)
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
    """Series lookup skips the deleted filter and maps the ORM row."""
    row = make_series(series_id=3, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    session = RecordingSession(results=[RecordingResult(scalar=row)])
    repository = make_series_repository(session)

    series = await repository.get_series(USER_ID, 3, include_deleted=True)
    sql = compile_statement(session.statements[-1])

    assert series == series_to_entity(row)
    assert "task_series.id = 3" in sql
    assert "task_series.user_id = 7" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_get_series_by_ids_is_tenant_scoped_and_optional_deleted() -> None:
    """Batch series lookup is tenant-scoped, mapped, and can include deleted rows."""
    row = make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_series_repository(session)

    series = await repository.get_series_by_ids(USER_ID, (1, 2), include_deleted=True)
    sql = compile_statement(session.statements[-1])

    assert series == {1: series_to_entity(row)}
    assert "task_series.user_id = 7" in sql
    assert "task_series.id IN (1, 2)" in sql
    assert "task_series.deleted_at IS NULL" not in sql


async def test_hard_delete_tasks_is_user_scoped_and_physical() -> None:
    """Hard deletion issues one owner-scoped DELETE for the requested tasks."""
    session = RecordingSession()
    repository = make_task_repository(session)
    await repository.hard_delete_tasks((4, 5), USER_ID)
    sql = compile_statement(session.statements[-1])
    assert sql.startswith("DELETE FROM tasks")
    assert "tasks.id IN (4, 5)" in sql
    assert "tasks.user_id = 7" in sql
    assert session.flush_count == 1


async def test_hard_delete_tasks_skips_empty_input() -> None:
    """Hard deletion with no identifiers performs no query or flush."""
    session = RecordingSession()
    repository = make_task_repository(session)

    await repository.hard_delete_tasks((), USER_ID)

    assert session.statements == []
    assert session.flush_count == 0


async def test_list_future_series_tasks_maps_domain_and_scopes_tenant() -> None:
    """Future series instances are owner-scoped, ordered, and mapped to domains."""
    row = make_task(task_id=11, series_id=3, due_at=NOW + timedelta(days=1))
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_task_repository(session)

    tasks = await repository.list_future_series_tasks(3, USER_ID, NOW)
    sql = compile_statement(session.statements[-1])

    assert list(tasks) == [task_to_entity(row)]
    assert "tasks.series_id = 3" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.completed_at IS NULL" in sql
    assert "tasks.due_at > " in sql
    assert "ORDER BY tasks.due_at, tasks.id" in sql


async def test_series_due_slot_exists_is_owner_scoped() -> None:
    """The slot lookup is owner-scoped and ignores the edited task."""
    session = RecordingSession(results=[RecordingResult(scalar=1)])
    repository = make_series_repository(session)

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


async def test_series_repository_lists_domain_series_and_scopes_tenant() -> None:
    """Series listing is tenant-scoped, ordered, and mapped to domains."""
    row = make_series(series_id=3, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_series_repository(session)

    series = await repository.list_series(USER_ID)
    sql = compile_statement(session.statements[-1])

    assert list(series) == [series_to_entity(row)]
    assert isinstance(series[0], TaskSeriesEntity)
    assert "task_series.user_id = 7" in sql
    assert "task_series.deleted_at IS NULL" in sql
    assert "ORDER BY task_series.id" in sql


async def test_series_repository_claim_maps_domain_when_locked() -> None:
    """A claimed generation series is returned as a domain entity."""
    row = make_series(series_id=3, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    session = RecordingSession(results=[RecordingResult(scalar=row)])
    repository = make_series_repository(session)

    series = await repository.claim_series_for_generation(3, USER_ID)

    assert series == series_to_entity(row)


async def test_series_task_locks_are_owner_scoped_and_ordered() -> None:
    """Series-wide task locks filter the owner, order instances, and map domains."""
    row = make_task(task_id=11, series_id=3, due_at=NOW)
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_series_repository(session)

    tasks = await repository.get_series_tasks_for_update(3, USER_ID)
    sql = compile_statement(session.statements[-1])

    assert list(tasks) == [task_to_entity(row)]
    assert "tasks.series_id = 3" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "ORDER BY tasks.due_at, tasks.id" in sql
    assert "FOR UPDATE" in sql
    assert session.flush_count == 0


async def test_series_add_inserts_fresh_row_and_propagates_generated_id() -> None:
    """New series insert a fresh ORM row and propagate the generated id."""
    session = RecordingSession(assigned_ids=[31])
    repository = make_series_repository(session)
    series = TaskSeriesEntity(
        user_id=USER_ID,
        title="New",
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=date(2026, 11, 1),
        frequency="periodic",
        interval_days=2,
    )

    result = await repository.add_series(series)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.id == 31
    assert row.user_id == USER_ID
    assert row.title == "New"
    assert row.frequency == "periodic"
    assert row.interval_days == 2
    assert isinstance(result, TaskSeriesEntity)
    assert result == series_to_entity(row)
    assert result.id == 31
    assert series.id == 31
    assert session.flush_count == 1


async def test_series_save_issues_update() -> None:
    """Saving a series applies the domain fields to the tracked row."""
    row = make_series(
        series_id=3,
        title="Old",
        time_of_day=time(9, 0),
        starts_on=date(2026, 11, 1),
    )
    session = RecordingSession(get_results=[row])
    repository = make_series_repository(session)
    series = TaskSeriesEntity(
        id=3,
        user_id=USER_ID,
        title="New",
        description="Updated",
        time_of_day=time(10, 0),
        timezone="Asia/Tokyo",
        starts_on=date(2026, 11, 2),
        frequency="periodic",
        interval_days=2,
    )

    await repository.save_series(series)

    assert row.title == "New"
    assert row.description == "Updated"
    assert row.time_of_day == time(10, 0)
    assert row.timezone == "Asia/Tokyo"
    assert row.frequency == "periodic"
    assert row.interval_days == 2
    assert session.statements == []
    assert session.flush_count == 1


async def test_series_save_transient_delegates_to_add() -> None:
    """Saving a transient series inserts it instead of updating."""
    session = RecordingSession(assigned_ids=[41])
    repository = make_series_repository(session)
    series = TaskSeriesEntity(
        user_id=USER_ID,
        title="New",
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=date(2026, 11, 1),
    )

    await repository.save_series(series)

    assert len(session.added) == 1
    assert session.added[0].title == "New"
    assert series.id == 41
    assert session.flush_count == 1


async def test_series_soft_delete_issues_scoped_update() -> None:
    """Soft deletion updates only the already owner-scoped series row."""
    session = RecordingSession()
    repository = make_series_repository(session)
    series = TaskSeriesEntity(
        id=3,
        user_id=USER_ID,
        title="Series",
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=date(2026, 11, 1),
    )

    await repository.soft_delete_series(series, NOW, BATCH_ID)
    sql = compile_statement(session.statements[-1])

    assert series.deleted_at == NOW
    assert series.deletion_batch_id == BATCH_ID
    assert sql.startswith("UPDATE task_series SET deleted_at=")
    assert "deletion_batch_id=" in sql
    assert "task_series.id = 3" in sql
    assert session.flush_count == 1


async def test_add_series_labels_stamps_owner_on_every_row() -> None:
    """Series label assignments persist the owner on every inserted row."""
    session = RecordingSession()
    repository = make_series_repository(session)

    await repository.add_series_labels(3, USER_ID, (1, 2))

    assert [(row.series_id, row.label_id, row.user_id) for row in session.added] == [
        (3, 1, USER_ID),
        (3, 2, USER_ID),
    ]
    assert session.flush_count == 1


async def test_replace_series_labels_deletes_then_reassigns() -> None:
    """Replacing series labels deletes the old selection and inserts the new one."""
    session = RecordingSession()
    repository = make_series_repository(session)

    await repository.replace_series_labels(3, USER_ID, (1, 2))

    delete_sql = compile_statement(session.statements[0])
    assert delete_sql.startswith("DELETE FROM series_labels")
    assert "series_labels.series_id = 3" in delete_sql
    assert "series_labels.user_id = 7" in delete_sql
    assert [(row.series_id, row.label_id) for row in session.added] == [(3, 1), (3, 2)]
    assert session.flush_count == 1


async def test_list_series_labels_returns_domain_labels() -> None:
    """Series label listing joins assignments and maps domain labels."""
    row = make_label(label_id=2)
    session = RecordingSession(results=[RecordingResult(values=[row])])
    repository = make_series_repository(session)

    result = await repository.list_series_labels(USER_ID, 3)
    sql = compile_statement(session.statements[-1])

    assert list(result) == [label_to_entity(row)]
    assert "series_labels.series_id = 3" in sql
    assert "series_labels.user_id = 7" in sql
    assert "labels.user_id = 7" in sql
    assert "labels.deleted_at IS NULL" in sql
    assert "ORDER BY labels.id" in sql


async def test_reminder_repository_lists_task_and_series_reminders() -> None:
    """Reminder listing is owner-scoped, ordered, and mapped to domains."""
    task_row = make_reminder(reminder_id=1, task_id=4)
    series_row = make_reminder(reminder_id=2, series_id=3)
    session = RecordingSession(
        results=[
            RecordingResult(values=[task_row]),
            RecordingResult(values=[series_row]),
        ]
    )
    repository = make_reminder_repository(session)

    task_reminders = await repository.list_task_reminders(USER_ID, 4)
    task_sql = compile_statement(session.statements[-1])

    series_reminders = await repository.list_series_reminders(USER_ID, 3)
    series_sql = compile_statement(session.statements[-1])

    assert list(task_reminders) == [reminder_to_entity(task_row)]
    assert list(series_reminders) == [reminder_to_entity(series_row)]
    assert "task_reminders.user_id = 7" in task_sql
    assert "task_reminders.task_id = 4" in task_sql
    assert "ORDER BY task_reminders.id" in task_sql
    assert "task_reminders.user_id = 7" in series_sql
    assert "task_reminders.series_id = 3" in series_sql
    assert "ORDER BY task_reminders.id" in series_sql


async def test_reminder_repository_claim_maps_domain_when_locked() -> None:
    """A claimed due reminder is returned as a domain entity."""
    row = make_reminder(reminder_id=11, task_id=4, next_run_at=NOW)
    session = RecordingSession(results=[RecordingResult(scalar=row)])
    repository = make_reminder_repository(session)

    reminder = await repository.claim_due_reminder(11, NOW)

    assert reminder == reminder_to_entity(row)


async def test_reminder_add_inserts_fresh_row_and_propagates_generated_id() -> None:
    """New reminders insert a fresh ORM row and propagate the generated id."""
    session = RecordingSession(assigned_ids=[11])
    repository = make_reminder_repository(session)
    reminder = TaskReminderEntity(
        user_id=USER_ID,
        task_id=4,
        kind="before_due",
        enabled=True,
        offset_minutes=30,
    )

    result = await repository.add_reminder(reminder)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.id == 11
    assert row.user_id == USER_ID
    assert row.task_id == 4
    assert row.kind == "before_due"
    assert isinstance(result, TaskReminderEntity)
    assert result == reminder_to_entity(row)
    assert result.id == 11
    assert reminder.id == 11
    assert session.flush_count == 1


async def test_reminder_save_issues_update() -> None:
    """Saving a reminder applies the domain fields to the tracked row."""
    row = make_reminder(reminder_id=4, task_id=9)
    session = RecordingSession(get_results=[row])
    repository = make_reminder_repository(session)
    reminder = TaskReminderEntity(
        id=4,
        user_id=USER_ID,
        task_id=9,
        kind="scheduled",
        enabled=False,
        interval_days=2,
        time_of_day=time(9, 0),
        timezone="Asia/Tokyo",
        next_run_at=NOW,
    )

    await repository.save_reminder(reminder)

    assert row.kind == "scheduled"
    assert row.enabled is False
    assert row.interval_days == 2
    assert row.time_of_day == time(9, 0)
    assert row.timezone == "Asia/Tokyo"
    assert row.next_run_at == NOW
    assert session.statements == []
    assert session.flush_count == 1


async def test_reminder_save_transient_delegates_to_add() -> None:
    """Saving a transient reminder inserts it instead of updating."""
    session = RecordingSession(assigned_ids=[51])
    repository = make_reminder_repository(session)
    reminder = TaskReminderEntity(user_id=USER_ID, task_id=4, kind="before_due")

    await repository.save_reminder(reminder)

    assert len(session.added) == 1
    assert session.added[0].task_id == 4
    assert reminder.id == 51
    assert session.flush_count == 1


async def test_reminder_deletes_are_owner_scoped() -> None:
    """Reminder deletion issues one owner-scoped DELETE per owner kind."""
    session = RecordingSession()
    repository = make_reminder_repository(session)

    await repository.delete_task_reminders(USER_ID, 4)
    task_sql = compile_statement(session.statements[-1])

    await repository.delete_series_reminders(USER_ID, 3)
    series_sql = compile_statement(session.statements[-1])

    assert task_sql.startswith("DELETE FROM task_reminders")
    assert "task_reminders.user_id = 7" in task_sql
    assert "task_reminders.task_id = 4" in task_sql
    assert series_sql.startswith("DELETE FROM task_reminders")
    assert "task_reminders.user_id = 7" in series_sql
    assert "task_reminders.series_id = 3" in series_sql
    assert session.flush_count == 2


async def test_cancel_task_and_series_reminders_clear_slots_and_deliveries() -> None:
    """Cancellation clears slots and active deliveries for tasks and series."""
    session = RecordingSession()
    repository = make_reminder_repository(session)

    await repository.cancel_task_reminders(USER_ID, 4)
    await repository.cancel_series_reminders(USER_ID, 3)

    assert len(session.statements) == 4
    assert session.flush_count == 2
    for reminder_sql, delivery_sql in zip(
        (
            compile_statement(session.statements[0]),
            compile_statement(session.statements[2]),
        ),
        (
            compile_statement(session.statements[1]),
            compile_statement(session.statements[3]),
        ),
        strict=True,
    ):
        assert reminder_sql.startswith("UPDATE task_reminders SET next_run_at=NULL")
        assert "task_reminders.user_id = 7" in reminder_sql
        assert delivery_sql.startswith("UPDATE reminder_deliveries SET status='cancelled'")
        assert "reminder_deliveries.user_id = 7" in delivery_sql
        assert "reminder_deliveries.status IN ('pending', 'failed', 'sending')" in delivery_sql
    task_reminder_sql = compile_statement(session.statements[0])
    series_reminder_sql = compile_statement(session.statements[2])
    assert "task_reminders.task_id = 4" in task_reminder_sql
    assert "task_reminders.series_id = 3" in series_reminder_sql


async def test_update_reminder_next_run_at_persists_slot() -> None:
    """Advancing a reminder updates its slot through one scoped UPDATE."""
    session = RecordingSession()
    repository = make_reminder_repository(session)
    reminder = TaskReminderEntity(
        id=4,
        user_id=USER_ID,
        task_id=9,
        kind="scheduled",
        timezone="UTC",
    )

    await repository.update_reminder_next_run_at(reminder, NOW)
    sql = compile_statement(session.statements[-1])

    assert reminder.next_run_at == NOW
    assert sql.startswith("UPDATE task_reminders SET next_run_at=")
    assert "task_reminders.id = 4" in sql
    assert session.flush_count == 1


async def test_get_delivery_maps_domain_and_uses_idempotency_key() -> None:
    """Delivery lookup uses the reminder slot key and maps a domain delivery."""
    row = make_delivery(delivery_id=5, reminder_id=4, scheduled_for=NOW)
    session = RecordingSession(results=[RecordingResult(scalar=row)])
    repository = make_reminder_repository(session)

    delivery = await repository.get_delivery(4, NOW)
    sql = compile_statement(session.statements[-1])

    assert delivery == delivery_to_entity(row)
    assert "reminder_deliveries.reminder_id = 4" in sql
    assert "reminder_deliveries.scheduled_for = " in sql


async def test_delivery_add_inserts_fresh_row_and_propagates_generated_id() -> None:
    """New deliveries insert a fresh ORM row and propagate the generated id."""
    session = RecordingSession(assigned_ids=[21])
    repository = make_reminder_repository(session)
    delivery = ReminderDeliveryEntity(
        reminder_id=4,
        user_id=USER_ID,
        scheduled_for=NOW,
    )

    result = await repository.add_delivery(delivery)

    assert len(session.added) == 1
    row = session.added[0]
    assert row.id == 21
    assert row.reminder_id == 4
    assert row.user_id == USER_ID
    assert row.scheduled_for == NOW
    assert isinstance(result, ReminderDeliveryEntity)
    assert result == delivery_to_entity(row)
    assert result.id == 21
    assert delivery.id == 21
    assert session.flush_count == 1


async def test_delivery_save_issues_update() -> None:
    """Saving a delivery applies the domain fields to the tracked row."""
    row = make_delivery(delivery_id=5, reminder_id=4, scheduled_for=NOW)
    session = RecordingSession(get_results=[row])
    repository = make_reminder_repository(session)
    delivery = ReminderDeliveryEntity(
        id=5,
        reminder_id=4,
        user_id=USER_ID,
        scheduled_for=NOW,
        status="sent",
        attempts=2,
        sent_at=NOW,
        telegram_message_id=99,
    )

    await repository.save_delivery(delivery)

    assert row.status == "sent"
    assert row.attempts == 2
    assert row.sent_at == NOW
    assert row.telegram_message_id == 99
    assert session.statements == []
    assert session.flush_count == 1


async def test_delivery_save_transient_delegates_to_add() -> None:
    """Saving a transient delivery inserts it instead of updating."""
    session = RecordingSession(assigned_ids=[61])
    repository = make_reminder_repository(session)
    delivery = ReminderDeliveryEntity(
        reminder_id=4,
        user_id=USER_ID,
        scheduled_for=NOW,
    )

    await repository.save_delivery(delivery)

    assert len(session.added) == 1
    assert session.added[0].reminder_id == 4
    assert delivery.id == 61
    assert session.flush_count == 1


async def test_delivery_state_transitions_are_persisted() -> None:
    """Every delivery transition flushes one scoped UPDATE without committing."""
    session = RecordingSession()
    repository = make_reminder_repository(session)
    delivery = ReminderDeliveryEntity(
        id=5,
        reminder_id=4,
        user_id=USER_ID,
        scheduled_for=NOW,
        attempts=2,
    )

    await repository.mark_delivery_sending(delivery, NOW)
    sending_sql = compile_statement(session.statements[-1])

    assert delivery.status == DeliveryStatus.SENDING.value
    assert delivery.locked_at == NOW
    assert delivery.attempts == 3
    assert delivery.next_attempt_at is None
    assert sending_sql.startswith("UPDATE reminder_deliveries SET status='sending'")
    assert "reminder_deliveries.id = 5" in sending_sql
    assert "locked_at=" in sending_sql
    assert "attempts=3" in sending_sql
    assert "next_attempt_at=NULL" in sending_sql

    await repository.mark_delivery_sent(delivery, NOW, 99)
    sent_sql = compile_statement(session.statements[-1])

    assert delivery.status == DeliveryStatus.SENT.value
    assert delivery.sent_at == NOW
    assert delivery.telegram_message_id == 99
    assert delivery.locked_at is None
    assert delivery.next_attempt_at is None
    assert delivery.last_error is None
    assert sent_sql.startswith("UPDATE reminder_deliveries SET status='sent'")
    assert "reminder_deliveries.id = 5" in sent_sql
    assert "sent_at=" in sent_sql
    assert "telegram_message_id=99" in sent_sql
    assert "locked_at=NULL" in sent_sql
    assert "next_attempt_at=NULL" in sent_sql
    assert "last_error=NULL" in sent_sql

    retry_at = NOW + timedelta(minutes=5)
    await repository.mark_delivery_pending(delivery, retry_at, "timeout")
    pending_sql = compile_statement(session.statements[-1])

    assert delivery.status == DeliveryStatus.PENDING.value
    assert delivery.next_attempt_at == retry_at
    assert delivery.locked_at is None
    assert delivery.last_error == "timeout"
    assert pending_sql.startswith("UPDATE reminder_deliveries SET status='pending'")
    assert "reminder_deliveries.id = 5" in pending_sql
    assert "next_attempt_at=" in pending_sql
    assert "locked_at=NULL" in pending_sql
    assert "last_error='timeout'" in pending_sql

    await repository.mark_delivery_failed(delivery, None, "fatal")
    failed_sql = compile_statement(session.statements[-1])

    assert delivery.status == DeliveryStatus.FAILED.value
    assert delivery.next_attempt_at is None
    assert delivery.locked_at is None
    assert delivery.last_error == "fatal"
    assert failed_sql.startswith("UPDATE reminder_deliveries SET status='failed'")
    assert "reminder_deliveries.id = 5" in failed_sql
    assert "next_attempt_at=NULL" in failed_sql
    assert "last_error='fatal'" in failed_sql

    await repository.cancel_delivery(delivery)
    cancel_sql = compile_statement(session.statements[-1])

    assert delivery.status == DeliveryStatus.CANCELLED.value
    assert delivery.locked_at is None
    assert delivery.next_attempt_at is None
    assert cancel_sql.startswith("UPDATE reminder_deliveries SET status='cancelled'")
    assert "reminder_deliveries.id = 5" in cancel_sql
    assert "locked_at=NULL" in cancel_sql
    assert "next_attempt_at=NULL" in cancel_sql

    await repository.reactivate_delivery(delivery)
    reactivate_sql = compile_statement(session.statements[-1])

    assert delivery.status == DeliveryStatus.PENDING.value
    assert delivery.attempts == 0
    assert delivery.locked_at is None
    assert delivery.next_attempt_at is None
    assert delivery.sent_at is None
    assert delivery.telegram_message_id is None
    assert delivery.last_error is None
    assert reactivate_sql.startswith("UPDATE reminder_deliveries SET status='pending'")
    assert "reminder_deliveries.id = 5" in reactivate_sql
    assert "attempts=0" in reactivate_sql
    assert "sent_at=NULL" in reactivate_sql
    assert "telegram_message_id=NULL" in reactivate_sql
    assert "last_error=NULL" in reactivate_sql
    assert session.flush_count == 6
