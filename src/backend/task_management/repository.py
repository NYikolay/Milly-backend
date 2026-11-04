from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.color_palette import ColorPalette
from backend.models.label import Label
from backend.models.project import Project
from backend.models.reminder_delivery import ReminderDelivery
from backend.models.series_label import SeriesLabel
from backend.models.task import Task
from backend.models.task_label import TaskLabel
from backend.models.task_reminder import TaskReminder
from backend.models.task_series import TaskSeries
from backend.models.user import User
from backend.task_management.enums import DeliveryStatus
from backend.task_management.schedule import HORIZON_DAYS


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
                func.count(Task.id),
                func.count(Task.id).filter(Task.completed_at.is_not(None)),
            ).where(
                Task.user_id == user_id,
                Task.due_at >= start,
                Task.due_at < end,
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
        await self._session.execute(
            update(Project)
            .where(
                Project.id == project.id,
                Project.user_id == project.user_id,
                Project.deleted_at.is_(None),
            )
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )

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

    async def get_user_timezone(self, user_id: int) -> str | None:
        """
        Load the IANA timezone of a user.

        Purpose:
            Convert local task dates and scheduled reminder slots to UTC.

        Args:
            user_id: Owner identifier.

        Returns:
            str | None: The stored timezone name, or ``None`` when the user is
                missing.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(select(User.timezone).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        user_id: int,
        *,
        date_start: datetime | None,
        date_end: datetime | None,
        project_ids: Sequence[int],
        priorities: Sequence[str],
        status: str,
        recurrence: str,
        query: str | None,
        include_archived_projects: bool,
        now: datetime,
    ) -> Sequence[Task]:
        """
        List non-deleted tasks using the service-level filters.

        Purpose:
            Power the task list with date, project, priority, status,
            recurrence, text, and archive filters.

        Args:
            user_id: Owner identifier.
            date_start: Inclusive lower bound for ``due_at``.
            date_end: Exclusive upper bound for ``due_at``.
            project_ids: Restrict to these project identifiers when non-empty.
            priorities: Restrict to these priority values when non-empty.
            status: One of ``all``, ``open``, ``completed``, or ``overdue``.
            recurrence: One of ``all``, ``regular``, or ``one_off``.
            query: Case-insensitive title/description substring.
            include_archived_projects: Whether tasks in archived projects are
                included.
            now: Current instant used to evaluate the overdue status.

        Returns:
            Sequence[Task]: Matching tasks ordered by ``(due_at, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(Task).where(
            Task.user_id == user_id,
            Task.deleted_at.is_(None),
        )
        if date_start is not None:
            statement = statement.where(Task.due_at >= date_start)
        if date_end is not None:
            statement = statement.where(Task.due_at < date_end)
        if project_ids:
            statement = statement.where(Task.project_id.in_(project_ids))
        if priorities:
            statement = statement.where(Task.priority.in_(priorities))
        if status == "open":
            statement = statement.where(Task.completed_at.is_(None))
        elif status == "completed":
            statement = statement.where(Task.completed_at.is_not(None))
        elif status == "overdue":
            statement = statement.where(
                Task.completed_at.is_(None),
                Task.due_at < now,
            )
        if recurrence == "regular":
            statement = statement.where(Task.series_id.is_not(None))
        elif recurrence == "one_off":
            statement = statement.where(Task.series_id.is_(None))
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    Task.title.ilike(pattern),
                    Task.description.ilike(pattern),
                )
            )
        if not include_archived_projects:
            active_project_ids = select(Project.id).where(
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
                Project.archived_at.is_(None),
            )
            statement = statement.where(
                or_(
                    Task.project_id.is_(None),
                    Task.project_id.in_(active_project_ids),
                )
            )
        result = await self._session.execute(statement.order_by(Task.due_at, Task.id))
        return result.scalars().all()

    async def get_task(
        self,
        user_id: int,
        task_id: int,
        *,
        for_update: bool = False,
    ) -> Task | None:
        """
        Load a non-deleted task owned by a user.

        Purpose:
            Resolve a task for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Task | None: The matching task, or ``None`` including when it
                belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(Task).where(
            Task.id == task_id,
            Task.user_id == user_id,
            Task.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def add_task(self, task: Task) -> Task:
        """
        Persist a new task.

        Purpose:
            Store a validated one-off task or generated series instance.

        Args:
            task: Task to insert.

        Returns:
            Task: The flushed task with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add(task)
        await self._session.flush()
        return task

    async def soft_delete_task(
        self,
        task: Task,
        deleted_at: datetime,
        batch_id: UUID | None = None,
    ) -> None:
        """
        Soft-delete a task.

        Purpose:
            Hide a task from reads while preserving its history.

        Args:
            task: Task to delete.
            deleted_at: Deletion timestamp.
            batch_id: Optional deletion batch shared with related rows.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        task.deleted_at = deleted_at
        task.deletion_batch_id = batch_id
        await self._session.flush()

    async def add_task_labels(
        self,
        task_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Assign labels to a task.

        Purpose:
            Copy a validated label selection into ``task_labels``.

        Args:
            task_id: Task identifier.
            user_id: Owner identifier.
            label_ids: Label identifiers to assign.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add_all(
            TaskLabel(task_id=task_id, label_id=label_id, user_id=user_id) for label_id in label_ids
        )
        await self._session.flush()

    async def replace_task_labels(
        self,
        task_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Replace all labels assigned to a task.

        Purpose:
            Apply an updated label selection to an existing task.

        Args:
            task_id: Task identifier.
            user_id: Owner identifier.
            label_ids: Complete new label selection.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        await self._session.execute(
            delete(TaskLabel).where(
                TaskLabel.task_id == task_id,
                TaskLabel.user_id == user_id,
            )
        )
        await self.add_task_labels(task_id, user_id, label_ids)

    async def list_task_labels(self, user_id: int, task_id: int) -> Sequence[Label]:
        """
        List non-deleted labels assigned to a task.

        Purpose:
            Build the label part of a task detail.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            Sequence[Label]: Assigned non-deleted labels ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(Label)
            .join(TaskLabel, TaskLabel.label_id == Label.id)
            .where(
                TaskLabel.task_id == task_id,
                TaskLabel.user_id == user_id,
                Label.user_id == user_id,
                Label.deleted_at.is_(None),
            )
            .order_by(Label.id)
        )
        return result.scalars().all()

    async def list_task_reminders(
        self,
        user_id: int,
        task_id: int,
    ) -> Sequence[TaskReminder]:
        """
        List reminders attached to a task.

        Purpose:
            Build the reminder part of a task detail and recompute slots.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            Sequence[TaskReminder]: Task reminders ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskReminder)
            .where(
                TaskReminder.user_id == user_id,
                TaskReminder.task_id == task_id,
            )
            .order_by(TaskReminder.id)
        )
        return result.scalars().all()

    async def list_series_reminders(
        self,
        user_id: int,
        series_id: int,
    ) -> Sequence[TaskReminder]:
        """
        List template reminders attached to a series.

        Purpose:
            Copy the reminder configuration into generated instances.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            Sequence[TaskReminder]: Template reminders ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskReminder)
            .where(
                TaskReminder.user_id == user_id,
                TaskReminder.series_id == series_id,
            )
            .order_by(TaskReminder.id)
        )
        return result.scalars().all()

    async def add_reminder(self, reminder: TaskReminder) -> TaskReminder:
        """
        Persist a task or series reminder.

        Purpose:
            Store a reminder rule in the current unit of work.

        Args:
            reminder: Reminder to insert.

        Returns:
            TaskReminder: The flushed reminder with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add(reminder)
        await self._session.flush()
        return reminder

    async def delete_task_reminders(self, user_id: int, task_id: int) -> None:
        """
        Delete every reminder attached to a task.

        Purpose:
            Rebuild task reminders after a due-time or configuration change.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        await self._session.execute(
            delete(TaskReminder).where(
                TaskReminder.user_id == user_id,
                TaskReminder.task_id == task_id,
            )
        )
        await self._session.flush()

    async def delete_series_reminders(self, user_id: int, series_id: int) -> None:
        """
        Delete every template reminder attached to a series.

        Purpose:
            Replace the reminder template after a series update.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        await self._session.execute(
            delete(TaskReminder).where(
                TaskReminder.user_id == user_id,
                TaskReminder.series_id == series_id,
            )
        )
        await self._session.flush()

    async def cancel_task_reminders(self, user_id: int, task_id: int) -> None:
        """
        Clear task reminder slots and cancel their active deliveries.

        Purpose:
            Stop delivery after completion, deletion, or schedule changes.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Sets ``next_run_at`` to ``None`` and cancels ``pending``,
              ``failed``, and ``sending`` deliveries.
            - Flushes but does not commit.
        """
        reminder_ids = select(TaskReminder.id).where(
            TaskReminder.user_id == user_id,
            TaskReminder.task_id == task_id,
        )
        await self._cancel_reminder_slots(user_id, reminder_ids)
        await self._session.flush()

    async def cancel_series_reminders(self, user_id: int, series_id: int) -> None:
        """
        Clear series reminder slots and cancel their active deliveries.

        Purpose:
            Stop delivery for template and instance reminders of a series.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Sets ``next_run_at`` to ``None`` and cancels ``pending``,
              ``failed``, and ``sending`` deliveries.
            - Flushes but does not commit.
        """
        reminder_ids = select(TaskReminder.id).where(
            TaskReminder.user_id == user_id,
            TaskReminder.series_id == series_id,
        )
        await self._cancel_reminder_slots(user_id, reminder_ids)
        await self._session.flush()

    async def _cancel_reminder_slots(
        self,
        user_id: int,
        reminder_ids: Any,
    ) -> None:
        """
        Clear slots and cancel active deliveries for a reminder selection.

        Purpose:
            Share the cancellation policy between task, series, and project
            pause operations.

        Args:
            user_id: Owner identifier.
            reminder_ids: Selectable reminder identifier subquery.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Does not flush; the public caller owns the flush.
        """
        await self._session.execute(
            update(TaskReminder)
            .where(TaskReminder.id.in_(reminder_ids))
            .values(next_run_at=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(ReminderDelivery)
            .where(
                ReminderDelivery.user_id == user_id,
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

    async def pause_project_reminders(self, user_id: int, project_id: int) -> None:
        """
        Pause reminders attached to a project.

        Purpose:
            Stop delivery while a project is archived.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Sets ``next_run_at`` to ``None`` for task and series reminders and
              cancels active deliveries.
            - Flushes but does not commit.
        """
        task_ids = select(Task.id).where(
            Task.user_id == user_id,
            Task.project_id == project_id,
        )
        series_ids = select(TaskSeries.id).where(
            TaskSeries.user_id == user_id,
            TaskSeries.project_id == project_id,
        )
        reminder_ids = select(TaskReminder.id).where(
            TaskReminder.user_id == user_id,
            or_(
                TaskReminder.task_id.in_(task_ids),
                TaskReminder.series_id.in_(series_ids),
            ),
        )
        await self._cancel_reminder_slots(user_id, reminder_ids)
        await self._session.flush()

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
            select(Project.id)
            .where(
                Project.id == project_id,
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
                Project.archived_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_series(self, user_id: int) -> Sequence[TaskSeries]:
        """
        List non-deleted series owned by a user.

        Purpose:
            Power the recurring-task list.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[TaskSeries]: Non-deleted series ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskSeries)
            .where(
                TaskSeries.user_id == user_id,
                TaskSeries.deleted_at.is_(None),
            )
            .order_by(TaskSeries.id)
        )
        return result.scalars().all()

    async def get_series(
        self,
        user_id: int,
        series_id: int,
        *,
        for_update: bool = False,
    ) -> TaskSeries | None:
        """
        Load a non-deleted series owned by a user.

        Purpose:
            Resolve a series for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            TaskSeries | None: The matching series, or ``None`` including when
                it belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(TaskSeries).where(
            TaskSeries.id == series_id,
            TaskSeries.user_id == user_id,
            TaskSeries.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def add_series(self, series: TaskSeries) -> TaskSeries:
        """
        Persist a new series.

        Purpose:
            Store a validated recurring-task template.

        Args:
            series: Series to insert.

        Returns:
            TaskSeries: The flushed series with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add(series)
        await self._session.flush()
        return series

    async def soft_delete_series(
        self,
        series: TaskSeries,
        deleted_at: datetime,
        batch_id: UUID | None = None,
    ) -> None:
        """
        Soft-delete a series.

        Purpose:
            Stop generation and hide the series from reads while preserving
            past instances.

        Args:
            series: Series to delete.
            deleted_at: Deletion timestamp.
            batch_id: Optional deletion batch shared with related rows.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        series.deleted_at = deleted_at
        series.deletion_batch_id = batch_id
        await self._session.flush()

    async def add_series_labels(
        self,
        series_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Assign labels to a series template.

        Purpose:
            Store the label set copied into generated instances.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            label_ids: Label identifiers to assign.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add_all(
            SeriesLabel(series_id=series_id, label_id=label_id, user_id=user_id)
            for label_id in label_ids
        )
        await self._session.flush()

    async def replace_series_labels(
        self,
        series_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Replace all labels assigned to a series template.

        Purpose:
            Apply an updated label selection to a series.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            label_ids: Complete new label selection.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        await self._session.execute(
            delete(SeriesLabel).where(
                SeriesLabel.series_id == series_id,
                SeriesLabel.user_id == user_id,
            )
        )
        await self.add_series_labels(series_id, user_id, label_ids)

    async def list_series_labels(self, user_id: int, series_id: int) -> Sequence[Label]:
        """
        List non-deleted labels assigned to a series.

        Purpose:
            Build the label part of a series detail.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            Sequence[Label]: Assigned non-deleted labels ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(Label)
            .join(SeriesLabel, SeriesLabel.label_id == Label.id)
            .where(
                SeriesLabel.series_id == series_id,
                SeriesLabel.user_id == user_id,
                Label.user_id == user_id,
                Label.deleted_at.is_(None),
            )
            .order_by(Label.id)
        )
        return result.scalars().all()

    async def list_series_for_generation(self, now: datetime) -> Sequence[TaskSeries]:
        """
        List series that may need new instances.

        Purpose:
            Select non-deleted series below the generation horizon whose
            project is active.

        Args:
            now: Current instant used to evaluate the 90-day horizon.

        Returns:
            Sequence[TaskSeries]: Eligible series ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        horizon = now + timedelta(days=HORIZON_DAYS)
        active_project_ids = select(Project.id).where(
            Project.deleted_at.is_(None),
            Project.archived_at.is_(None),
        )
        result = await self._session.execute(
            select(TaskSeries)
            .where(
                TaskSeries.deleted_at.is_(None),
                or_(
                    TaskSeries.generated_through.is_(None),
                    TaskSeries.generated_through < horizon,
                ),
                or_(
                    TaskSeries.project_id.is_(None),
                    TaskSeries.project_id.in_(active_project_ids),
                ),
            )
            .order_by(TaskSeries.id)
        )
        return result.scalars().all()

    async def get_series_tasks_for_update(
        self,
        series_id: int,
        user_id: int,
    ) -> Sequence[Task]:
        """
        Load non-deleted tasks of a series with a row lock.

        Purpose:
            Serialize series-wide updates and deletions.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.

        Returns:
            Sequence[Task]: Non-deleted series tasks ordered by due time.

        Notes:
            - Locks the selected task rows until the transaction ends.
            - Flushes but does not commit.
        """
        result = await self._session.execute(
            select(Task)
            .where(
                Task.series_id == series_id,
                Task.user_id == user_id,
                Task.deleted_at.is_(None),
            )
            .order_by(Task.due_at, Task.id)
            .with_for_update()
        )
        return result.scalars().all()

    async def list_future_series_tasks(
        self,
        series_id: int,
        user_id: int,
        now: datetime,
    ) -> Sequence[Task]:
        """
        List future, incomplete, non-deleted instances of a series.

        Purpose:
            Select exactly the instances affected by series edits and deletes.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            now: Current instant used as the lower bound.

        Returns:
            Sequence[Task]: Matching tasks ordered by due time.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(Task)
            .where(
                Task.series_id == series_id,
                Task.user_id == user_id,
                Task.deleted_at.is_(None),
                Task.completed_at.is_(None),
                Task.due_at > now,
            )
            .order_by(Task.due_at, Task.id)
        )
        return result.scalars().all()

    async def list_series_due_times(
        self,
        series_id: int,
        user_id: int,
    ) -> Sequence[datetime]:
        """
        List due times already generated for a series.

        Purpose:
            Make instance generation idempotent for existing slots.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.

        Returns:
            Sequence[datetime]: Due instants of non-deleted series tasks.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(Task.due_at).where(
                Task.series_id == series_id,
                Task.user_id == user_id,
                Task.deleted_at.is_(None),
            )
        )
        return result.scalars().all()

    async def list_due_reminders(self, now: datetime) -> Sequence[TaskReminder]:
        """
        List due, enabled reminders with active owners.

        Purpose:
            Feed the reminder dispatcher only rules that may be delivered.

        Args:
            now: Current instant used to select due slots.

        Returns:
            Sequence[TaskReminder]: Due reminders ordered by slot and
                identifier.

        Notes:
            - Requires the reminder to be enabled, its master toggle to be on,
              its task or series to be active, and its project not to be
              archived or deleted.
            - Performs no writes and does not commit.
        """
        active_project_ids = select(Project.id).where(
            Project.deleted_at.is_(None),
            Project.archived_at.is_(None),
        )
        task_exists = (
            select(Task.id)
            .where(
                Task.id == TaskReminder.task_id,
                Task.user_id == TaskReminder.user_id,
                Task.deleted_at.is_(None),
                Task.completed_at.is_(None),
                Task.reminders_enabled.is_(True),
                or_(
                    Task.project_id.is_(None),
                    Task.project_id.in_(active_project_ids),
                ),
            )
            .exists()
        )
        series_exists = (
            select(TaskSeries.id)
            .where(
                TaskSeries.id == TaskReminder.series_id,
                TaskSeries.user_id == TaskReminder.user_id,
                TaskSeries.deleted_at.is_(None),
                TaskSeries.reminders_enabled.is_(True),
                or_(
                    TaskSeries.project_id.is_(None),
                    TaskSeries.project_id.in_(active_project_ids),
                ),
            )
            .exists()
        )
        result = await self._session.execute(
            select(TaskReminder)
            .where(
                TaskReminder.enabled.is_(True),
                TaskReminder.next_run_at.is_not(None),
                TaskReminder.next_run_at <= now,
                or_(task_exists, series_exists),
            )
            .order_by(TaskReminder.next_run_at, TaskReminder.id)
        )
        return result.scalars().all()

    async def claim_due_reminder(
        self,
        reminder_id: int,
        now: datetime,
    ) -> TaskReminder | None:
        """
        Claim one due reminder for delivery.

        Purpose:
            Serialize workers with ``FOR UPDATE SKIP LOCKED`` so two workers do
            not deliver the same reminder slot.

        Args:
            reminder_id: Reminder identifier.
            now: Current instant used to verify the slot is still due.

        Returns:
            TaskReminder | None: The locked reminder, or ``None`` when it is no
                longer due or was claimed by another worker.

        Notes:
            - Locks the ``task_reminders`` row until the transaction ends.
            - Flushes but does not commit; the dispatcher commits the claim.
        """
        result = await self._session.execute(
            select(TaskReminder)
            .where(
                TaskReminder.id == reminder_id,
                TaskReminder.enabled.is_(True),
                TaskReminder.next_run_at.is_not(None),
                TaskReminder.next_run_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        return result.scalar_one_or_none()

    async def update_reminder_next_run_at(
        self,
        reminder: TaskReminder,
        next_run_at: datetime | None,
    ) -> None:
        """
        Update a reminder's next slot.

        Purpose:
            Advance or clear a slot after a successful or final delivery.

        Args:
            reminder: Reminder to update.
            next_run_at: New slot, or ``None`` to stop delivery.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        reminder.next_run_at = next_run_at
        await self._session.flush()

    async def get_delivery(
        self,
        reminder_id: int,
        scheduled_for: datetime,
    ) -> ReminderDelivery | None:
        """
        Load a delivery by its idempotency key.

        Purpose:
            Detect sent, cancelled, and retryable deliveries for a slot.

        Args:
            reminder_id: Reminder identifier.
            scheduled_for: Scheduled slot.

        Returns:
            ReminderDelivery | None: The matching delivery, or ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ReminderDelivery).where(
                ReminderDelivery.reminder_id == reminder_id,
                ReminderDelivery.scheduled_for == scheduled_for,
            )
        )
        return result.scalar_one_or_none()

    async def add_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        """
        Persist a new delivery.

        Purpose:
            Create the idempotent delivery row for a reminder slot.

        Args:
            delivery: Delivery to insert.

        Returns:
            ReminderDelivery: The flushed delivery with its generated
                identifier.

        Notes:
            - Flushes but does not commit.
        """
        self._session.add(delivery)
        await self._session.flush()
        return delivery

    async def mark_delivery_sending(
        self,
        delivery: ReminderDelivery,
        now: datetime,
    ) -> None:
        """
        Claim a delivery for sending.

        Purpose:
            Record the worker lease and increment the attempt counter.

        Args:
            delivery: Delivery to claim.
            now: Current instant written to ``locked_at``.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Sets ``status='sending'`` and clears ``next_attempt_at``.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.SENDING.value
        delivery.locked_at = now
        delivery.attempts += 1
        delivery.next_attempt_at = None
        await self._session.flush()

    async def mark_delivery_sent(
        self,
        delivery: ReminderDelivery,
        sent_at: datetime,
        telegram_message_id: int | None,
    ) -> None:
        """
        Mark a delivery as sent.

        Purpose:
            Record successful Telegram delivery and clear the worker lease.

        Args:
            delivery: Delivery to update.
            sent_at: Success timestamp.
            telegram_message_id: Identifier returned by Telegram, if any.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.SENT.value
        delivery.sent_at = sent_at
        delivery.telegram_message_id = telegram_message_id
        delivery.locked_at = None
        delivery.next_attempt_at = None
        delivery.last_error = None
        await self._session.flush()

    async def mark_delivery_pending(
        self,
        delivery: ReminderDelivery,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> None:
        """
        Mark a delivery for retry.

        Purpose:
            Apply a backoff after a retryable sender failure.

        Args:
            delivery: Delivery to update.
            next_attempt_at: Earliest retry instant.
            last_error: Safe short failure description.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Clears ``locked_at`` and flushes without committing.
        """
        delivery.status = DeliveryStatus.PENDING.value
        delivery.next_attempt_at = next_attempt_at
        delivery.locked_at = None
        delivery.last_error = last_error
        await self._session.flush()

    async def mark_delivery_failed(
        self,
        delivery: ReminderDelivery,
        next_attempt_at: datetime | None,
        last_error: str | None,
    ) -> None:
        """
        Mark a delivery as finally failed.

        Purpose:
            Stop retries after the attempt budget is exhausted.

        Args:
            delivery: Delivery to update.
            next_attempt_at: Optional final retry instant; usually ``None``.
            last_error: Safe short failure description.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Clears ``locked_at`` and flushes without committing.
        """
        delivery.status = DeliveryStatus.FAILED.value
        delivery.next_attempt_at = next_attempt_at
        delivery.locked_at = None
        delivery.last_error = last_error
        await self._session.flush()

    async def reactivate_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Reset a cancelled delivery to pending.

        Purpose:
            Reuse the unique ``(reminder_id, scheduled_for)`` row when a slot
            becomes deliverable again.

        Args:
            delivery: Cancelled delivery to reactivate.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Resets attempts, locks, retry slots, and send metadata.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.PENDING.value
        delivery.attempts = 0
        delivery.locked_at = None
        delivery.next_attempt_at = None
        delivery.sent_at = None
        delivery.telegram_message_id = None
        delivery.last_error = None
        await self._session.flush()

    async def commit(self) -> None:
        """
        Commit the current transaction.

        Purpose:
            Release the reminder claim before sending so the delivery lease is
            visible to other workers.

        Returns:
            None: The transaction is committed.

        Notes:
            - Only the dispatcher calls this explicitly.
        """
        await self._session.commit()

    async def flush(self) -> None:
        """
        Flush pending changes.

        Purpose:
            Persist in-place entity mutations performed by services.

        Returns:
            None: The current unit of work is flushed.

        Notes:
            - Does not commit.
        """
        await self._session.flush()
