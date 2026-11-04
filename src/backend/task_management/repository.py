from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.color_palette import ColorPalette
from backend.models.label import Label
from backend.models.project import Project
from backend.models.reminder_delivery import ReminderDelivery
from backend.models.task import Task
from backend.models.task_reminder import TaskReminder
from backend.models.task_series import TaskSeries
from backend.task_management.enums import DeliveryStatus


class SqlAlchemyTaskManagementRepository:
    """SQLAlchemy implementation of the task-management repository contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the repository with a request-scoped session.

        Purpose:
            Bind palette, project, and label persistence operations to the
            caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The repository never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

    async def list_palette_colors(self) -> Sequence[ColorPalette]:
        """
        List every palette color in display order.

        Purpose:
            Expose the complete global palette for administrative consumers.

        Returns:
            Sequence[ColorPalette]: Colors ordered by ``(sort_order, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPalette).order_by(ColorPalette.sort_order, ColorPalette.id)
        )
        return result.scalars().all()

    async def list_active_palette_colors(self) -> Sequence[ColorPalette]:
        """
        List active palette colors in display order.

        Purpose:
            Provide the selectable client palette.

        Returns:
            Sequence[ColorPalette]: Active colors ordered by
                ``(sort_order, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPalette)
            .where(ColorPalette.is_active.is_(True))
            .order_by(ColorPalette.sort_order, ColorPalette.id)
        )
        return result.scalars().all()

    async def get_palette_color(self, color_id: int) -> ColorPalette | None:
        """
        Load one palette color by identifier.

        Purpose:
            Validate an explicit color selection and resolve display data.

        Args:
            color_id: Palette color identifier.

        Returns:
            ColorPalette | None: The matching color, or ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPalette).where(ColorPalette.id == color_id)
        )
        return result.scalar_one_or_none()

    async def get_default_palette_color(self) -> ColorPalette | None:
        """
        Load the first active palette color.

        Purpose:
            Resolve the color used when a project or label omits ``color_id``.

        Returns:
            ColorPalette | None: The first active color by
                ``(sort_order, id)``, or ``None`` when no color is active.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPalette)
            .where(ColorPalette.is_active.is_(True))
            .order_by(ColorPalette.sort_order, ColorPalette.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def count_active_palette_colors(self) -> int:
        """
        Count active palette colors.

        Purpose:
            Enforce the invariant that at least one palette color stays active.

        Returns:
            int: Number of colors whose ``is_active`` flag is set.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(func.count()).select_from(ColorPalette).where(ColorPalette.is_active.is_(True))
        )
        return int(result.scalar_one())

    async def palette_color_is_used(self, color_id: int) -> bool:
        """
        Check whether a palette color is referenced by a project or label.

        Purpose:
            Protect colors from deletion while foreign keys reference them.

        Args:
            color_id: Palette color identifier.

        Returns:
            bool: ``True`` when any project or label, including soft-deleted
                rows, references the color.

        Notes:
            - Performs no writes and does not commit.
        """
        project_exists = select(Project.id).where(Project.color_id == color_id).exists()
        label_exists = select(Label.id).where(Label.color_id == color_id).exists()
        result = await self._session.execute(select(or_(project_exists, label_exists)))
        return bool(result.scalar_one())

    async def list_projects(
        self,
        user_id: int,
        *,
        include_archived: bool,
    ) -> Sequence[Project]:
        """
        List non-deleted projects owned by a user.

        Purpose:
            Power the active project list and the project archive.

        Args:
            user_id: Owner identifier.
            include_archived: Whether archived projects are included.

        Returns:
            Sequence[Project]: Non-deleted projects with active projects first,
                then case-insensitive name and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(Project).where(
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
        )
        if not include_archived:
            statement = statement.where(Project.archived_at.is_(None))
        result = await self._session.execute(
            statement.order_by(
                Project.archived_at.asc().nulls_first(),
                func.lower(Project.name),
                Project.id,
            )
        )
        return result.scalars().all()

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
            Project | None: The matching project, or ``None`` including when it
                belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(Project).where(
            Project.id == project_id,
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

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
        statement = select(Project.id).where(
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
            Project.archived_at.is_(None),
            func.lower(Project.name) == name.lower(),
        )
        if exclude_project_id is not None:
            statement = statement.where(Project.id != exclude_project_id)
        result = await self._session.execute(statement.limit(1))
        return result.scalar_one_or_none() is not None

    async def add_project(self, project: Project) -> Project:
        """
        Persist a new project.

        Purpose:
            Store a validated project in the current unit of work.

        Args:
            project: Project to insert.

        Returns:
            Project: The flushed project with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add(project)
        await self._session.flush()
        return project

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
            Project.archived_at.is_not(None) if archived else Project.archived_at.is_(None)
        )
        result = await self._session.execute(
            select(func.count())
            .select_from(Project)
            .where(
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
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
                func.count(Task.id),
                func.count(Task.id).filter(Task.completed_at.is_not(None)),
            ).where(
                Task.user_id == user_id,
                Task.project_id == project_id,
                Task.deleted_at.is_(None),
            )
        )
        total, completed = result.one()
        return int(total), int(completed)

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
            project: Project to delete.
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
            - Does not commit; the request dependency owns the transaction.
        """
        project.deleted_at = deleted_at
        project.deletion_batch_id = batch_id

        task_ids = select(Task.id).where(
            Task.project_id == project.id,
            Task.user_id == project.user_id,
        )
        series_ids = select(TaskSeries.id).where(
            TaskSeries.project_id == project.id,
            TaskSeries.user_id == project.user_id,
        )
        reminder_ids = select(TaskReminder.id).where(
            TaskReminder.user_id == project.user_id,
            or_(
                TaskReminder.task_id.in_(task_ids),
                TaskReminder.series_id.in_(series_ids),
            ),
        )

        await self._session.execute(
            update(TaskSeries)
            .where(
                TaskSeries.project_id == project.id,
                TaskSeries.user_id == project.user_id,
                TaskSeries.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(Task)
            .where(
                Task.project_id == project.id,
                Task.user_id == project.user_id,
                Task.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(TaskReminder)
            .where(TaskReminder.id.in_(reminder_ids))
            .values(next_run_at=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(ReminderDelivery)
            .where(
                ReminderDelivery.user_id == project.user_id,
                ReminderDelivery.reminder_id.in_(reminder_ids),
                ReminderDelivery.status.in_(
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

    async def list_labels(self, user_id: int) -> Sequence[Label]:
        """
        List non-deleted labels owned by a user.

        Purpose:
            Power label selection and label management.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[Label]: Non-deleted labels ordered by case-insensitive
                name and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(Label)
            .where(
                Label.user_id == user_id,
                Label.deleted_at.is_(None),
            )
            .order_by(func.lower(Label.name), Label.id)
        )
        return result.scalars().all()

    async def get_label(
        self,
        user_id: int,
        label_id: int,
        *,
        for_update: bool = False,
    ) -> Label | None:
        """
        Load a non-deleted label owned by a user.

        Purpose:
            Resolve a label for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Label | None: The matching label, or ``None`` including when it
                belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(Label).where(
            Label.id == label_id,
            Label.user_id == user_id,
            Label.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def get_labels_by_ids(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> Sequence[Label]:
        """
        Load multiple non-deleted labels owned by a user.

        Purpose:
            Validate label selections before assigning them to tasks or series.

        Args:
            user_id: Owner identifier.
            label_ids: Requested label identifiers.

        Returns:
            Sequence[Label]: Matching owned labels; unknown or soft-deleted
                identifiers are omitted.

        Notes:
            - Performs no writes and does not commit.
        """
        if not label_ids:
            return []
        result = await self._session.execute(
            select(Label)
            .where(
                Label.user_id == user_id,
                Label.deleted_at.is_(None),
                Label.id.in_(label_ids),
            )
            .order_by(Label.id)
        )
        return result.scalars().all()

    async def label_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_label_id: int | None = None,
    ) -> bool:
        """
        Check for an active label name owned by a user.

        Purpose:
            Enforce case-insensitive uniqueness among non-deleted labels before
            writing.

        Args:
            user_id: Owner identifier.
            name: Candidate label name, already trimmed by the caller.
            exclude_label_id: Label to ignore during the comparison.

        Returns:
            bool: ``True`` when another non-deleted label has the same
                case-insensitive name.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(Label.id).where(
            Label.user_id == user_id,
            Label.deleted_at.is_(None),
            func.lower(Label.name) == name.lower(),
        )
        if exclude_label_id is not None:
            statement = statement.where(Label.id != exclude_label_id)
        result = await self._session.execute(statement.limit(1))
        return result.scalar_one_or_none() is not None

    async def add_label(self, label: Label) -> Label:
        """
        Persist a new label.

        Purpose:
            Store a validated label in the current unit of work.

        Args:
            label: Label to insert.

        Returns:
            Label: The flushed label with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add(label)
        await self._session.flush()
        return label

    async def soft_delete_label(self, label: Label, deleted_at: datetime) -> None:
        """
        Soft-delete a label.

        Purpose:
            Hide a label from selection and task responses while preserving
            its task and series assignments.

        Args:
            label: Label to delete.
            deleted_at: Deletion timestamp.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit; the request dependency owns the
              transaction.
        """
        label.deleted_at = deleted_at
        await self._session.flush()

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
        active_project_ids = select(Project.id).where(
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
            Project.archived_at.is_(None),
        )
        result = await self._session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.user_id == user_id,
                Task.deleted_at.is_(None),
                Task.completed_at.is_(None),
                or_(
                    Task.project_id.is_(None),
                    Task.project_id.in_(active_project_ids),
                ),
            )
        )
        return int(result.scalar_one())
