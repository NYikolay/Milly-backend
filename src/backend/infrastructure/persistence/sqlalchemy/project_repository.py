"""SQLAlchemy implementation of the project persistence port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import Project
from backend.domain.enums import DeliveryStatus
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_project,
    project_to_entity,
)
from backend.models.project import Project as ProjectRow
from backend.models.reminder_delivery import ReminderDelivery as ReminderDeliveryRow
from backend.models.task import Task as TaskRow
from backend.models.task_reminder import TaskReminder as TaskReminderRow
from backend.models.task_series import TaskSeries as TaskSeriesRow


class SqlAlchemyProjectRepository:
    """SQLAlchemy implementation of the project persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the project adapter with a request-scoped session.

        Purpose:
            Bind project reads and writes to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

    async def list_projects(
        self,
        user_id: int,
        *,
        include_archived: bool,
        limit: int,
        after_archived: bool | None,
        after_name: str | None,
        after_project_id: int | None,
    ) -> Sequence[tuple[Project, str]]:
        """
        List non-deleted projects owned by a user, at most one page.

        Purpose:
            Power the active project list and the project archive with
            deterministic cursor pagination.

        Args:
            user_id: Owner identifier.
            include_archived: Whether archived projects are included.
            limit: Maximum number of projects to return.
            after_archived: Cursor archive state of the last project on the
                previous page.
            after_name: Cursor case-insensitive name key of the last project on
                the previous page.
            after_project_id: Cursor identifier of the last project on the
                previous page.

        Returns:
            Sequence[tuple[Project, str]]: Non-deleted domain projects ordered
                by ``(is_archived, lower(name), id)`` paired with the
                ``lower(name)`` key produced by SQL.

        Notes:
            - The cursor predicate is applied only when every cursor value is
              provided.
            - The returned name key is the exact database value used for
              ordering so cursors remain consistent for non-ASCII names.
            - Performs no writes and does not commit.
        """
        archived_rank = case((ProjectRow.archived_at.is_(None), 0), else_=1)
        name_key = func.lower(ProjectRow.name)
        statement = select(ProjectRow, name_key.label("name_key")).where(
            ProjectRow.user_id == user_id,
            ProjectRow.deleted_at.is_(None),
        )
        if not include_archived:
            statement = statement.where(ProjectRow.archived_at.is_(None))
        if after_archived is not None and after_name is not None and after_project_id is not None:
            after_rank = 0 if not after_archived else 1
            statement = statement.where(
                or_(
                    archived_rank > after_rank,
                    and_(
                        archived_rank == after_rank,
                        name_key > after_name,
                    ),
                    and_(
                        archived_rank == after_rank,
                        name_key == after_name,
                        ProjectRow.id > after_project_id,
                    ),
                )
            )
        result = await self._session.execute(
            statement.order_by(archived_rank, name_key, ProjectRow.id).limit(limit)
        )
        rows: list[tuple[Project, str]] = []
        for project, lower_name in result.all():
            rows.append((project_to_entity(cast(ProjectRow, project)), cast(str, lower_name)))
        return rows

    async def get_project(
        self,
        user_id: int,
        project_id: int,
        *,
        for_update: bool = False,
    ) -> Project | None:
        """
        Load a non-deleted project owned by a user.

        Purpose:
            Resolve a project for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Project | None: The matching domain project, or ``None`` including
                when it belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(ProjectRow).where(
            ProjectRow.id == project_id,
            ProjectRow.user_id == user_id,
            ProjectRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return project_to_entity(row) if row is not None else None

    async def get_projects_by_ids(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, Project]:
        """
        Load non-deleted projects grouped by identifier.

        Purpose:
            Enrich a page of task details with project data in one query.

        Args:
            user_id: Owner identifier.
            project_ids: Project identifiers to load.

        Returns:
            dict[int, Project]: Owned non-deleted domain projects keyed by
                identifier; missing or cross-user projects are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not project_ids:
            return {}
        result = await self._session.execute(
            select(ProjectRow).where(
                ProjectRow.user_id == user_id,
                ProjectRow.deleted_at.is_(None),
                ProjectRow.id.in_(project_ids),
            )
        )
        return {row.id: project_to_entity(row) for row in result.scalars().all()}

    async def project_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_project_id: int | None = None,
    ) -> bool:
        """
        Check for an active project name owned by a user.

        Purpose:
            Enforce case-insensitive uniqueness among non-deleted,
            non-archived projects before writing.

        Args:
            user_id: Owner identifier.
            name: Candidate project name, already trimmed by the caller.
            exclude_project_id: Project to ignore during the comparison.

        Returns:
            bool: ``True`` when another active project has the same
                case-insensitive name.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(ProjectRow.id).where(
            ProjectRow.user_id == user_id,
            ProjectRow.deleted_at.is_(None),
            ProjectRow.archived_at.is_(None),
            func.lower(ProjectRow.name) == name.lower(),
        )
        if exclude_project_id is not None:
            statement = statement.where(ProjectRow.id != exclude_project_id)
        result = await self._session.execute(statement.limit(1))
        return result.scalar_one_or_none() is not None

    async def add_project(self, project: Project) -> Project:
        """
        Persist a new project.

        Purpose:
            Store a validated project in the current unit of work.

        Args:
            project: Domain project to insert.

        Returns:
            Project: The flushed project mapped back from its ORM row with the
                generated identifier.

        Notes:
            - Inserts a fresh ORM row and never copies the transient domain
              identifier, so the database identity generates the key.
            - Flushes but does not commit.
        """
        row = _new_project_row(project)
        self._session.add(row)
        await self._session.flush()
        return project_to_entity(row)

    async def save_project(self, project: Project) -> None:
        """
        Persist changes to an existing project.

        Purpose:
            Make service-level mutations of a loaded domain project durable in
            the current unit of work.

        Args:
            project: Project whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Raises:
            LookupError: If the project row is not present in the session.

        Notes:
            - A transient project with ``id == 0`` is inserted through
              ``add_project``.
            - Flushes but does not commit.
        """
        if project.id == 0:
            await self.add_project(project)
            return
        row = await self._session.get(ProjectRow, project.id)
        if row is None:
            raise LookupError(f"Project {project.id} is not present in the session")
        apply_project(project, row)
        await self._session.flush()

    async def count_projects(self, user_id: int, *, archived: bool) -> int:
        """
        Count non-deleted projects in one archive state.

        Purpose:
            Provide dashboard statistics for active and archived projects.

        Args:
            user_id: Owner identifier.
            archived: Whether to count archived instead of active projects.

        Returns:
            int: Number of matching non-deleted projects.

        Notes:
            - Performs no writes and does not commit.
        """
        archived_filter = (
            ProjectRow.archived_at.is_not(None) if archived else ProjectRow.archived_at.is_(None)
        )
        result = await self._session.execute(
            select(func.count())
            .select_from(ProjectRow)
            .where(
                ProjectRow.user_id == user_id,
                ProjectRow.deleted_at.is_(None),
                archived_filter,
            )
        )
        return int(result.scalar_one())

    async def project_progress(self, user_id: int, project_id: int) -> tuple[int, int]:
        """
        Count all and completed tasks of a project.

        Purpose:
            Build the project progress summary.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            tuple[int, int]: Total non-deleted task count and the subset with a
                completion timestamp.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(
                func.count(TaskRow.id),
                func.count(TaskRow.id).filter(TaskRow.completed_at.is_not(None)),
            ).where(
                TaskRow.user_id == user_id,
                TaskRow.project_id == project_id,
                TaskRow.deleted_at.is_(None),
            )
        )
        total, completed = result.one()
        return int(total), int(completed)

    async def project_progress_for_projects(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, tuple[int, int]]:
        """
        Count all and completed tasks grouped by project.

        Purpose:
            Build a project page's progress summaries without one query per
            project.

        Args:
            user_id: Owner identifier.
            project_ids: Project identifiers to summarize.

        Returns:
            dict[int, tuple[int, int]]: Total non-deleted task count and the
                subset with a completion timestamp, keyed by project
                identifier; projects without tasks are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not project_ids:
            return {}
        result = await self._session.execute(
            select(
                TaskRow.project_id,
                func.count(TaskRow.id),
                func.count(TaskRow.id).filter(TaskRow.completed_at.is_not(None)),
            )
            .where(
                TaskRow.user_id == user_id,
                TaskRow.deleted_at.is_(None),
                TaskRow.project_id.in_(project_ids),
            )
            .group_by(TaskRow.project_id)
        )
        progress: dict[int, tuple[int, int]] = {}
        for project_id, total, completed in result.all():
            if project_id is None:
                continue
            progress[project_id] = (int(total), int(completed))
        return progress

    async def today_progress(
        self,
        user_id: int,
        start: datetime,
        end: datetime,
    ) -> tuple[int, int]:
        """
        Count all and completed tasks due within a time window.

        Purpose:
            Build the "today" progress summary from UTC bounds of the user's
            local day.

        Args:
            user_id: Owner identifier.
            start: Inclusive lower bound for ``tasks.due_at``.
            end: Exclusive upper bound for ``tasks.due_at``.

        Returns:
            tuple[int, int]: Total non-deleted task count and the subset with a
                completion timestamp.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(
                func.count(TaskRow.id),
                func.count(TaskRow.id).filter(TaskRow.completed_at.is_not(None)),
            ).where(
                TaskRow.user_id == user_id,
                TaskRow.due_at >= start,
                TaskRow.due_at < end,
                TaskRow.deleted_at.is_(None),
            )
        )
        total, completed = result.one()
        return int(total), int(completed)

    async def planned_tasks_count(self, user_id: int) -> int:
        """
        Count incomplete tasks that belong to active plans.

        Purpose:
            Provide the dashboard count of planned tasks, including tasks
            without a project but excluding tasks in archived or deleted
            projects.

        Args:
            user_id: Owner identifier.

        Returns:
            int: Number of non-deleted, incomplete tasks in active projects or
                without a project.

        Notes:
            - Performs no writes and does not commit.
        """
        active_project_ids = select(ProjectRow.id).where(
            ProjectRow.user_id == user_id,
            ProjectRow.deleted_at.is_(None),
            ProjectRow.archived_at.is_(None),
        )
        result = await self._session.execute(
            select(func.count())
            .select_from(TaskRow)
            .where(
                TaskRow.user_id == user_id,
                TaskRow.deleted_at.is_(None),
                TaskRow.completed_at.is_(None),
                or_(
                    TaskRow.project_id.is_(None),
                    TaskRow.project_id.in_(active_project_ids),
                ),
            )
        )
        return int(result.scalar_one())

    async def project_is_active(self, user_id: int, project_id: int) -> bool:
        """
        Check whether a project is active.

        Purpose:
            Gate reminder delivery and series generation on an unarchived,
            non-deleted project.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            bool: ``True`` when the project exists, is not deleted, and is not
                archived.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ProjectRow.id)
            .where(
                ProjectRow.id == project_id,
                ProjectRow.user_id == user_id,
                ProjectRow.deleted_at.is_(None),
                ProjectRow.archived_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def soft_delete_project(
        self,
        project: Project,
        batch_id: UUID,
        deleted_at: datetime,
    ) -> None:
        """
        Soft-delete a project, its series, tasks, and pending deliveries.

        Purpose:
            Apply the project deletion batch atomically so deleted projects,
            generated series, future instances, and reminders stop being
            visible or deliverable.

        Args:
            project: Domain project to delete.
            batch_id: Identifier shared by every row deleted in this operation.
            deleted_at: Deletion timestamp written to every affected row.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Updates the project, its non-deleted series, and its non-deleted
              tasks with ``deleted_at`` and ``deletion_batch_id``.
            - Cancels ``pending``, ``failed``, and ``sending`` deliveries of
              reminders attached to the project's tasks and series, clearing
              their locks and retry slots.
            - Does not commit; the UnitOfWork owns the transaction.
        """
        await self._session.execute(
            update(ProjectRow)
            .where(
                ProjectRow.id == project.id,
                ProjectRow.user_id == project.user_id,
                ProjectRow.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )

        task_ids = select(TaskRow.id).where(
            TaskRow.project_id == project.id,
            TaskRow.user_id == project.user_id,
        )
        series_ids = select(TaskSeriesRow.id).where(
            TaskSeriesRow.project_id == project.id,
            TaskSeriesRow.user_id == project.user_id,
        )
        reminder_ids = select(TaskReminderRow.id).where(
            TaskReminderRow.user_id == project.user_id,
            or_(
                TaskReminderRow.task_id.in_(task_ids),
                TaskReminderRow.series_id.in_(series_ids),
            ),
        )

        await self._session.execute(
            update(TaskSeriesRow)
            .where(
                TaskSeriesRow.project_id == project.id,
                TaskSeriesRow.user_id == project.user_id,
                TaskSeriesRow.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(TaskRow)
            .where(
                TaskRow.project_id == project.id,
                TaskRow.user_id == project.user_id,
                TaskRow.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(TaskReminderRow)
            .where(TaskReminderRow.id.in_(reminder_ids))
            .values(next_run_at=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(
                ReminderDeliveryRow.user_id == project.user_id,
                ReminderDeliveryRow.reminder_id.in_(reminder_ids),
                ReminderDeliveryRow.status.in_(
                    (
                        DeliveryStatus.PENDING.value,
                        DeliveryStatus.FAILED.value,
                        DeliveryStatus.SENDING.value,
                    )
                ),
            )
            .values(
                status=DeliveryStatus.CANCELLED.value,
                locked_at=None,
                next_attempt_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()


def _new_project_row(project: Project) -> ProjectRow:
    """
    Build a fresh ORM project row from a domain project.

    Purpose:
        Insert new projects without copying the transient domain identifier, so
        PostgreSQL can generate the primary key.

    Args:
        project: Domain project to insert.

    Returns:
        ProjectRow: A detached ORM row populated with persisted fields.

    Notes:
        - The returned row is not added to a session and has no identifier.
    """
    return ProjectRow(
        user_id=project.user_id,
        name=project.name,
        description=project.description,
        color_id=project.color_id,
        icon=project.icon,
        archived_at=project.archived_at,
        deleted_at=project.deleted_at,
        deletion_batch_id=project.deletion_batch_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
