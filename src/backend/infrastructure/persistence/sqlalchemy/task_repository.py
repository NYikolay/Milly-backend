"""SQLAlchemy implementation of the task persistence port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import Label, Task, TaskLabel, TaskReminder
from backend.domain.enums import SeriesFrequency
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_task,
    label_to_entity,
    reminder_to_entity,
    task_to_entity,
)
from backend.models.label import Label as LabelRow
from backend.models.project import Project as ProjectRow
from backend.models.task import Task as TaskRow
from backend.models.task_label import TaskLabel as TaskLabelRow
from backend.models.task_reminder import TaskReminder as TaskReminderRow
from backend.models.task_series import TaskSeries as TaskSeriesRow


class SqlAlchemyTaskRepository:
    """SQLAlchemy implementation of the task persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the task adapter with a request-scoped session.

        Purpose:
            Bind task reads and writes to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

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
        limit: int,
        after_due_at: datetime | None,
        after_task_id: int | None,
    ) -> Sequence[Task]:
        """
        List non-deleted tasks using the service-level filters.

        Purpose:
            Power the task list with date, project, priority, status,
            recurrence, text, and archive filters, returning at most one page.

        Args:
            user_id: Owner identifier.
            date_start: Inclusive lower bound for ``due_at``.
            date_end: Exclusive upper bound for ``due_at``.
            project_ids: Restrict to these project identifiers when non-empty.
            priorities: Restrict to these priority values when non-empty.
            status: One of ``all``, ``open``, ``completed``, or ``overdue``.
            recurrence: One of ``all``, ``regular``, ``periodic``, or
                ``one_off``; ``regular`` matches daily series and ``periodic``
                matches interval series.
            query: Case-insensitive title/description substring.
            include_archived_projects: Whether tasks in archived projects are
                included.
            now: Current instant used to evaluate the overdue status.
            limit: Maximum number of tasks to return.
            after_due_at: Cursor due time of the last task on the previous page.
            after_task_id: Cursor identifier of the last task on the previous
                page.

        Returns:
            Sequence[Task]: Matching domain tasks ordered by ``(due_at, id)``.

        Notes:
            - The cursor predicate is applied only when both cursor values are
              provided.
            - Soft-deleted series still determine the recurrence of their
              generated tasks.
            - Performs no writes and does not commit.
        """
        statement = select(TaskRow).where(
            TaskRow.user_id == user_id,
            TaskRow.deleted_at.is_(None),
        )
        if date_start is not None:
            statement = statement.where(TaskRow.due_at >= date_start)
        if date_end is not None:
            statement = statement.where(TaskRow.due_at < date_end)
        if project_ids:
            statement = statement.where(TaskRow.project_id.in_(project_ids))
        if priorities:
            statement = statement.where(TaskRow.priority.in_(priorities))
        if status == "open":
            statement = statement.where(TaskRow.completed_at.is_(None))
        elif status == "completed":
            statement = statement.where(TaskRow.completed_at.is_not(None))
        elif status == "overdue":
            statement = statement.where(
                TaskRow.completed_at.is_(None),
                TaskRow.due_at < now,
            )
        if recurrence == "regular":
            statement = statement.join(
                TaskSeriesRow,
                and_(
                    TaskSeriesRow.id == TaskRow.series_id,
                    TaskSeriesRow.user_id == user_id,
                ),
            ).where(TaskSeriesRow.frequency == SeriesFrequency.DAILY.value)
        elif recurrence == "periodic":
            statement = statement.join(
                TaskSeriesRow,
                and_(
                    TaskSeriesRow.id == TaskRow.series_id,
                    TaskSeriesRow.user_id == user_id,
                ),
            ).where(TaskSeriesRow.frequency == SeriesFrequency.PERIODIC.value)
        elif recurrence == "one_off":
            statement = statement.where(TaskRow.series_id.is_(None))
        if query:
            pattern = f"%{query}%"
            statement = statement.where(
                or_(
                    TaskRow.title.ilike(pattern),
                    TaskRow.description.ilike(pattern),
                )
            )
        if not include_archived_projects:
            active_project_ids = select(ProjectRow.id).where(
                ProjectRow.user_id == user_id,
                ProjectRow.deleted_at.is_(None),
                ProjectRow.archived_at.is_(None),
            )
            statement = statement.where(
                or_(
                    TaskRow.project_id.is_(None),
                    TaskRow.project_id.in_(active_project_ids),
                )
            )
        if after_due_at is not None and after_task_id is not None:
            statement = statement.where(
                or_(
                    TaskRow.due_at > after_due_at,
                    and_(
                        TaskRow.due_at == after_due_at,
                        TaskRow.id > after_task_id,
                    ),
                )
            )
        result = await self._session.execute(
            statement.order_by(TaskRow.due_at, TaskRow.id).limit(limit)
        )
        return [task_to_entity(row) for row in result.scalars().all()]

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
            Task | None: The matching domain task, or ``None`` including when
                it belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(TaskRow).where(
            TaskRow.id == task_id,
            TaskRow.user_id == user_id,
            TaskRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return task_to_entity(row) if row is not None else None

    async def add_task(self, task: Task) -> Task:
        """
        Persist a new task.

        Purpose:
            Store a validated one-off task or generated series instance.

        Args:
            task: Domain task to insert.

        Returns:
            Task: The flushed task mapped back from its ORM row with the
                generated identifier.

        Notes:
            - Inserts a fresh ORM row and never copies the transient domain
              identifier, so the database identity generates the key.
            - Flushes but does not commit.
        """
        row = _new_task_row(task)
        self._session.add(row)
        await self._session.flush()
        return task_to_entity(row)

    async def add_tasks(self, tasks: Sequence[Task]) -> Sequence[Task]:
        """
        Persist multiple new tasks in one batch.

        Purpose:
            Materialize generated series instances without one flush per task.

        Args:
            tasks: Domain tasks to insert.

        Returns:
            Sequence[Task]: The inserted tasks mapped back from their ORM rows
                with generated identifiers.

        Notes:
            - Empty input is a no-op and does not flush.
            - Inserts fresh ORM rows and never copies transient domain
              identifiers.
            - Flushes once but does not commit.
        """
        if not tasks:
            return tasks
        rows = [_new_task_row(task) for task in tasks]
        self._session.add_all(rows)
        await self._session.flush()
        return [task_to_entity(row) for row in rows]

    async def save_task(self, task: Task) -> None:
        """
        Persist changes to an existing task.

        Purpose:
            Make service-level mutations of a loaded domain task durable in the
            current unit of work.

        Args:
            task: Task whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Raises:
            LookupError: If the task row is not present in the session.

        Notes:
            - A transient task with ``id == 0`` is inserted through
              ``add_task``.
            - Resolves the tracked row by primary key; the caller is
              responsible for loading the entity through a tenant-scoped read.
            - Flushes but does not commit.
        """
        if task.id == 0:
            await self.add_task(task)
            return
        row = await self._session.get(TaskRow, task.id)
        if row is None:
            raise LookupError(f"Task {task.id} is not present in the session")
        apply_task(task, row)
        await self._session.flush()

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
            task: Domain task to delete.
            deleted_at: Deletion timestamp.
            batch_id: Optional deletion batch shared with related rows.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Updates the task row by primary key, matching the previous
              ORM-flush behavior for an already tenant-scoped entity.
            - Flushes but does not commit.
        """
        task.deleted_at = deleted_at
        task.deletion_batch_id = batch_id
        await self._session.execute(
            update(TaskRow)
            .where(TaskRow.id == task.id)
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

    async def hard_delete_tasks(self, task_ids: Sequence[int], user_id: int) -> None:
        """
        Physically delete owned tasks and their dependent rows.

        Purpose:
            Remove generated instances that no longer belong to a series edit
            or a deleted series while relying on foreign-key cascades for
            labels, reminders, and deliveries.

        Args:
            task_ids: Task identifiers to delete.
            user_id: Owner identifier; tasks of other users are unaffected.

        Returns:
            None: The rows are removed from the current unit of work.

        Notes:
            - Empty input is a no-op and does not flush.
            - Deletes tasks physically, unlike ``soft_delete_task``.
            - Dependent task labels, reminders, and deliveries are removed by
              database cascades.
            - Flushes but does not commit.
        """
        if not task_ids:
            return
        await self._session.execute(
            delete(TaskRow).where(
                TaskRow.id.in_(task_ids),
                TaskRow.user_id == user_id,
            )
        )
        await self._session.flush()

    async def list_task_labels_for_tasks(
        self,
        user_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Sequence[Label]]:
        """
        List non-deleted labels grouped by task.

        Purpose:
            Enrich a page of task details without one label query per task.

        Args:
            user_id: Owner identifier.
            task_ids: Task identifiers to enrich.

        Returns:
            dict[int, Sequence[Label]]: Assigned domain labels keyed by task
                identifier; tasks without labels are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not task_ids:
            return {}
        result = await self._session.execute(
            select(LabelRow, TaskLabelRow.task_id)
            .join(TaskLabelRow, TaskLabelRow.label_id == LabelRow.id)
            .where(
                TaskLabelRow.user_id == user_id,
                TaskLabelRow.task_id.in_(task_ids),
                LabelRow.user_id == user_id,
                LabelRow.deleted_at.is_(None),
            )
            .order_by(TaskLabelRow.task_id, LabelRow.id)
        )
        grouped: dict[int, Sequence[Label]] = {}
        for label_row, task_id in result.all():
            grouped[task_id] = (*grouped.get(task_id, ()), label_to_entity(label_row))
        return grouped

    async def list_task_reminders_for_tasks(
        self,
        user_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Sequence[TaskReminder]]:
        """
        List reminders grouped by task.

        Purpose:
            Enrich a page of task details without one reminder query per task.

        Args:
            user_id: Owner identifier.
            task_ids: Task identifiers to enrich.

        Returns:
            dict[int, Sequence[TaskReminder]]: Domain reminders keyed by task
                identifier; tasks without reminders are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not task_ids:
            return {}
        result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.task_id.in_(task_ids),
            )
            .order_by(TaskReminderRow.task_id, TaskReminderRow.id)
        )
        grouped: dict[int, Sequence[TaskReminder]] = {}
        for reminder_row in result.scalars().all():
            if reminder_row.task_id is None:
                continue
            grouped[reminder_row.task_id] = (
                *grouped.get(reminder_row.task_id, ()),
                reminder_to_entity(reminder_row),
            )
        return grouped

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
            - Flushes but does not commit, including for an empty selection.
        """
        self._session.add_all(
            TaskLabelRow(task_id=task_id, label_id=label_id, user_id=user_id)
            for label_id in label_ids
        )
        await self._session.flush()

    async def add_task_label_rows(self, rows: Sequence[TaskLabel]) -> None:
        """
        Persist multiple task-label assignments in one batch.

        Purpose:
            Copy the series label set onto every generated instance.

        Args:
            rows: Domain task-label assignment rows to insert.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Empty input is a no-op and does not flush.
            - Flushes once but does not commit.
        """
        if not rows:
            return
        self._session.add_all([_new_task_label_row(row) for row in rows])
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
            - Deletes owned assignments first, then inserts the new selection
              through ``add_task_labels``.
            - Flushes but does not commit.
        """
        await self._session.execute(
            delete(TaskLabelRow).where(
                TaskLabelRow.task_id == task_id,
                TaskLabelRow.user_id == user_id,
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
            Sequence[Label]: Assigned non-deleted domain labels ordered by
                identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(LabelRow)
            .join(TaskLabelRow, TaskLabelRow.label_id == LabelRow.id)
            .where(
                TaskLabelRow.task_id == task_id,
                TaskLabelRow.user_id == user_id,
                LabelRow.user_id == user_id,
                LabelRow.deleted_at.is_(None),
            )
            .order_by(LabelRow.id)
        )
        return [label_to_entity(row) for row in result.scalars().all()]

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
            Sequence[Task]: Matching domain tasks ordered by due time.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskRow)
            .where(
                TaskRow.series_id == series_id,
                TaskRow.user_id == user_id,
                TaskRow.deleted_at.is_(None),
                TaskRow.completed_at.is_(None),
                TaskRow.due_at > now,
            )
            .order_by(TaskRow.due_at, TaskRow.id)
        )
        return [task_to_entity(row) for row in result.scalars().all()]


def _new_task_row(task: Task) -> TaskRow:
    """
    Build a fresh ORM task row from a domain task.

    Purpose:
        Insert new tasks without copying the transient domain identifier, so
        PostgreSQL can generate the primary key.

    Args:
        task: Domain task to insert.

    Returns:
        TaskRow: A transient ORM row populated with persisted fields.

    Notes:
        - The returned row is not added to a session and has no identifier.
    """
    return TaskRow(
        user_id=task.user_id,
        series_id=task.series_id,
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        due_at=task.due_at,
        completed_at=task.completed_at,
        reminders_enabled=task.reminders_enabled,
        deleted_at=task.deleted_at,
        deletion_batch_id=task.deletion_batch_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _new_task_label_row(row: TaskLabel) -> TaskLabelRow:
    """
    Build an ORM task-label row from a domain assignment.

    Purpose:
        Insert generated-instance label assignments from domain entities.

    Args:
        row: Domain task-label assignment to insert.

    Returns:
        TaskLabelRow: A transient ORM row populated with persisted fields.

    Notes:
        - The returned row is not added to a session.
    """
    return TaskLabelRow(
        task_id=row.task_id,
        label_id=row.label_id,
        user_id=row.user_id,
        created_at=row.created_at,
    )
