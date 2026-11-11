"""SQLAlchemy implementation of the reminder persistence port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import ReminderDelivery, TaskReminder
from backend.domain.enums import DeliveryStatus
from backend.domain.task_management.models import ProjectReminderContext
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_delivery,
    apply_reminder,
    delivery_to_entity,
    reminder_to_entity,
    series_to_entity,
    task_to_entity,
)
from backend.models.project import Project as ProjectRow
from backend.models.reminder_delivery import ReminderDelivery as ReminderDeliveryRow
from backend.models.task import Task as TaskRow
from backend.models.task_reminder import TaskReminder as TaskReminderRow
from backend.models.task_series import TaskSeries as TaskSeriesRow


class SqlAlchemyReminderRepository:
    """SQLAlchemy implementation of the reminder persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the reminder adapter with a request-scoped session.

        Purpose:
            Bind reminder reads, delivery generation, and delivery transitions
            to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

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
            Sequence[TaskReminder]: Domain task reminders ordered by
                identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.task_id == task_id,
            )
            .order_by(TaskReminderRow.id)
        )
        return [reminder_to_entity(row) for row in result.scalars().all()]

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
            Sequence[TaskReminder]: Template domain reminders ordered by
                identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.series_id == series_id,
            )
            .order_by(TaskReminderRow.id)
        )
        return [reminder_to_entity(row) for row in result.scalars().all()]

    async def add_reminder(self, reminder: TaskReminder) -> TaskReminder:
        """
        Persist a task or series reminder.

        Purpose:
            Store a reminder rule in the current unit of work.

        Args:
            reminder: Domain reminder to insert.

        Returns:
            TaskReminder: The flushed reminder mapped back from its ORM row
                with the generated identifier.

        Notes:
            - Inserts a fresh ORM row and never copies the transient domain
              identifier, so the database identity generates the key.
            - Writes the generated identifier back to the input entity.
            - Flushes but does not commit.
        """
        row = _new_reminder_row(reminder)
        self._session.add(row)
        await self._session.flush()
        if row.id is not None:
            reminder.id = row.id
        return reminder_to_entity(row)

    async def add_reminders(self, reminders: Sequence[TaskReminder]) -> None:
        """
        Persist multiple reminders in one batch.

        Purpose:
            Copy template reminders onto generated series instances without one
            flush per reminder.

        Args:
            reminders: Domain reminders to insert.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Empty input is a no-op and does not flush.
            - Inserts fresh ORM rows and writes generated identifiers back to
              the input entities.
            - Flushes once but does not commit.
        """
        if not reminders:
            return
        rows = [_new_reminder_row(reminder) for reminder in reminders]
        self._session.add_all(rows)
        await self._session.flush()
        for reminder, row in zip(reminders, rows, strict=True):
            if row.id is not None:
                reminder.id = row.id

    async def save_reminder(self, reminder: TaskReminder) -> None:
        """
        Persist changes to an existing reminder.

        Purpose:
            Make service-level mutations of a loaded domain reminder durable in
            the current unit of work.

        Args:
            reminder: Reminder whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Raises:
            LookupError: If the reminder row is not present in the session.

        Notes:
            - A transient reminder with ``id == 0`` is inserted through
              ``add_reminder``.
            - Resolves the tracked row by primary key; the caller is
              responsible for loading the entity through a tenant-scoped read.
            - Flushes but does not commit.
        """
        if reminder.id == 0:
            await self.add_reminder(reminder)
            return
        row = await self._session.get(TaskReminderRow, reminder.id)
        if row is None:
            raise LookupError(f"Reminder {reminder.id} is not present in the session")
        apply_reminder(reminder, row)
        await self._session.flush()

    async def save_reminders(self, reminders: Sequence[TaskReminder]) -> None:
        """
        Persist changes to a batch of existing reminders.

        Purpose:
            Apply project resume slot changes to every loaded reminder with one
            flush instead of one flush per reminder.

        Args:
            reminders: Reminders whose persisted fields changed.

        Returns:
            None: The changes stay in the current unit of work.

        Raises:
            LookupError: If any reminder row is not present in the session.

        Notes:
            - Resolves every tracked row by primary key; the caller is
              responsible for loading the entities through a tenant-scoped
              read.
            - Empty input still flushes once so the project resume transaction
              keeps its previous flush count.
            - Flushes once but does not commit.
        """
        for reminder in reminders:
            row = await self._session.get(TaskReminderRow, reminder.id)
            if row is None:
                raise LookupError(f"Reminder {reminder.id} is not present in the session")
            apply_reminder(reminder, row)
        await self._session.flush()

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
            delete(TaskReminderRow).where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.task_id == task_id,
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
            delete(TaskReminderRow).where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.series_id == series_id,
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
        reminder_ids = select(TaskReminderRow.id).where(
            TaskReminderRow.user_id == user_id,
            TaskReminderRow.task_id == task_id,
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
        reminder_ids = select(TaskReminderRow.id).where(
            TaskReminderRow.user_id == user_id,
            TaskReminderRow.series_id == series_id,
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
            update(TaskReminderRow)
            .where(TaskReminderRow.id.in_(reminder_ids))
            .values(next_run_at=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(
                ReminderDeliveryRow.user_id == user_id,
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
        task_ids = select(TaskRow.id).where(
            TaskRow.user_id == user_id,
            TaskRow.project_id == project_id,
        )
        series_ids = select(TaskSeriesRow.id).where(
            TaskSeriesRow.user_id == user_id,
            TaskSeriesRow.project_id == project_id,
        )
        reminder_ids = select(TaskReminderRow.id).where(
            TaskReminderRow.user_id == user_id,
            or_(
                TaskReminderRow.task_id.in_(task_ids),
                TaskReminderRow.series_id.in_(series_ids),
            ),
        )
        await self._cancel_reminder_slots(user_id, reminder_ids)
        await self._session.flush()

    async def list_project_reminder_contexts(
        self,
        user_id: int,
        project_id: int,
    ) -> Sequence[ProjectReminderContext]:
        """
        List reminder contexts for an archived project.

        Purpose:
            Load the project series and task reminders together with the data
            the application resume policy needs to recompute their slots.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            Sequence[ProjectReminderContext]: Reminder contexts for non-deleted
                project series and incomplete non-deleted project tasks.

        Notes:
            - Loads project series, project tasks, and both reminder
              collections in four batched queries.
            - Does not mutate entities, does not flush, and does not commit.
        """
        series_result = await self._session.execute(
            select(TaskSeriesRow).where(
                TaskSeriesRow.user_id == user_id,
                TaskSeriesRow.project_id == project_id,
                TaskSeriesRow.deleted_at.is_(None),
            )
        )
        series_by_id = {
            series.id: series for series in map(series_to_entity, series_result.scalars().all())
        }
        series_reminders_result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.series_id.in_(list(series_by_id)),
            )
            .order_by(TaskReminderRow.id)
        )
        series_contexts = [
            ProjectReminderContext(
                reminder=reminder_to_entity(row),
                task_due_at=None,
                series_timezone=series_by_id[row.series_id].timezone,
                is_series=True,
            )
            for row in series_reminders_result.scalars().all()
            if row.series_id is not None
        ]

        task_result = await self._session.execute(
            select(TaskRow).where(
                TaskRow.user_id == user_id,
                TaskRow.project_id == project_id,
                TaskRow.deleted_at.is_(None),
                TaskRow.completed_at.is_(None),
            )
        )
        task_by_id = {task.id: task for task in map(task_to_entity, task_result.scalars().all())}
        task_reminders_result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.user_id == user_id,
                TaskReminderRow.task_id.in_(list(task_by_id)),
            )
            .order_by(TaskReminderRow.id)
        )
        task_contexts = [
            ProjectReminderContext(
                reminder=reminder_to_entity(row),
                task_due_at=task_by_id[row.task_id].due_at,
                series_timezone=None,
                is_series=False,
            )
            for row in task_reminders_result.scalars().all()
            if row.task_id is not None
        ]
        return [*series_contexts, *task_contexts]

    async def list_due_reminders(
        self,
        now: datetime,
        *,
        limit: int,
    ) -> Sequence[TaskReminder]:
        """
        List due, enabled reminders with active owners.

        Purpose:
            Feed the reminder dispatcher only rules that may be delivered.

        Args:
            now: Current instant used to select due slots.
            limit: Maximum number of reminders to return.

        Returns:
            Sequence[TaskReminder]: Due domain reminders ordered by slot and
                identifier.

        Notes:
            - Requires the reminder to be enabled, its master toggle to be on,
              its task or series to be active, and its project not to be
              archived or deleted.
            - Performs no writes and does not commit.
        """
        active_project_ids = select(ProjectRow.id).where(
            ProjectRow.deleted_at.is_(None),
            ProjectRow.archived_at.is_(None),
        )
        task_exists = (
            select(TaskRow.id)
            .where(
                TaskRow.id == TaskReminderRow.task_id,
                TaskRow.user_id == TaskReminderRow.user_id,
                TaskRow.deleted_at.is_(None),
                TaskRow.completed_at.is_(None),
                TaskRow.reminders_enabled.is_(True),
                or_(
                    TaskRow.project_id.is_(None),
                    TaskRow.project_id.in_(active_project_ids),
                ),
            )
            .exists()
        )
        series_exists = (
            select(TaskSeriesRow.id)
            .where(
                TaskSeriesRow.id == TaskReminderRow.series_id,
                TaskSeriesRow.user_id == TaskReminderRow.user_id,
                TaskSeriesRow.deleted_at.is_(None),
                TaskSeriesRow.reminders_enabled.is_(True),
                or_(
                    TaskSeriesRow.project_id.is_(None),
                    TaskSeriesRow.project_id.in_(active_project_ids),
                ),
            )
            .exists()
        )
        result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.enabled.is_(True),
                TaskReminderRow.next_run_at.is_not(None),
                TaskReminderRow.next_run_at <= now,
                or_(task_exists, series_exists),
            )
            .order_by(TaskReminderRow.next_run_at, TaskReminderRow.id)
            .limit(limit)
        )
        return [reminder_to_entity(row) for row in result.scalars().all()]

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
            TaskReminder | None: The locked domain reminder, or ``None`` when
                it is no longer due or was claimed by another worker.

        Notes:
            - Locks the ``task_reminders`` row until the transaction ends.
            - Does not flush or commit; the dispatcher commits the claim.
        """
        result = await self._session.execute(
            select(TaskReminderRow)
            .where(
                TaskReminderRow.id == reminder_id,
                TaskReminderRow.enabled.is_(True),
                TaskReminderRow.next_run_at.is_not(None),
                TaskReminderRow.next_run_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        row = result.scalar_one_or_none()
        return reminder_to_entity(row) if row is not None else None

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
            reminder: Domain reminder to update.
            next_run_at: New slot, or ``None`` to stop delivery.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Updates the reminder row by primary key, matching the previous
              ORM-flush behavior for an already loaded entity.
            - Flushes but does not commit.
        """
        reminder.next_run_at = next_run_at
        await self._session.execute(
            update(TaskReminderRow)
            .where(TaskReminderRow.id == reminder.id)
            .values(next_run_at=next_run_at)
            .execution_options(synchronize_session=False)
        )
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
            ReminderDelivery | None: The matching domain delivery, or ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ReminderDeliveryRow).where(
                ReminderDeliveryRow.reminder_id == reminder_id,
                ReminderDeliveryRow.scheduled_for == scheduled_for,
            )
        )
        row = result.scalar_one_or_none()
        return delivery_to_entity(row) if row is not None else None

    async def add_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        """
        Persist a new delivery.

        Purpose:
            Create the idempotent delivery row for a reminder slot.

        Args:
            delivery: Domain delivery to insert.

        Returns:
            ReminderDelivery: The flushed delivery mapped back from its ORM row
                with the generated identifier.

        Notes:
            - Inserts a fresh ORM row and never copies the transient domain
              identifier, so the database identity generates the key.
            - Writes the generated identifier back to the input entity.
            - Flushes but does not commit.
        """
        row = _new_delivery_row(delivery)
        self._session.add(row)
        await self._session.flush()
        if row.id is not None:
            delivery.id = row.id
        return delivery_to_entity(row)

    async def save_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Persist changes to an existing delivery.

        Purpose:
            Make service-level mutations of a loaded domain delivery durable in
            the current unit of work.

        Args:
            delivery: Delivery whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Raises:
            LookupError: If the delivery row is not present in the session.

        Notes:
            - A transient delivery with ``id == 0`` is inserted through
              ``add_delivery``.
            - Resolves the tracked row by primary key; the caller is
              responsible for loading the entity through a reminder-scoped
              read.
            - Flushes but does not commit.
        """
        if delivery.id == 0:
            await self.add_delivery(delivery)
            return
        row = await self._session.get(ReminderDeliveryRow, delivery.id)
        if row is None:
            raise LookupError(f"Delivery {delivery.id} is not present in the session")
        apply_delivery(delivery, row)
        await self._session.flush()

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
            delivery: Domain delivery to claim.
            now: Current instant written to ``locked_at``.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Sets ``status='sending'`` and clears ``next_attempt_at``.
            - Updates the delivery row by primary key without loading it again.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.SENDING.value
        delivery.locked_at = now
        delivery.attempts += 1
        delivery.next_attempt_at = None
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(ReminderDeliveryRow.id == delivery.id)
            .values(
                status=delivery.status,
                locked_at=delivery.locked_at,
                attempts=delivery.attempts,
                next_attempt_at=delivery.next_attempt_at,
            )
            .execution_options(synchronize_session=False)
        )
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
            delivery: Domain delivery to update.
            sent_at: Success timestamp.
            telegram_message_id: Identifier returned by Telegram, if any.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Updates the delivery row by primary key without loading it again.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.SENT.value
        delivery.sent_at = sent_at
        delivery.telegram_message_id = telegram_message_id
        delivery.locked_at = None
        delivery.next_attempt_at = None
        delivery.last_error = None
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(ReminderDeliveryRow.id == delivery.id)
            .values(
                status=delivery.status,
                sent_at=delivery.sent_at,
                telegram_message_id=delivery.telegram_message_id,
                locked_at=delivery.locked_at,
                next_attempt_at=delivery.next_attempt_at,
                last_error=delivery.last_error,
            )
            .execution_options(synchronize_session=False)
        )
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
            delivery: Domain delivery to update.
            next_attempt_at: Earliest retry instant.
            last_error: Safe short failure description.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Clears ``locked_at`` and updates the row by primary key.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.PENDING.value
        delivery.next_attempt_at = next_attempt_at
        delivery.locked_at = None
        delivery.last_error = last_error
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(ReminderDeliveryRow.id == delivery.id)
            .values(
                status=delivery.status,
                next_attempt_at=delivery.next_attempt_at,
                locked_at=delivery.locked_at,
                last_error=delivery.last_error,
            )
            .execution_options(synchronize_session=False)
        )
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
            delivery: Domain delivery to update.
            next_attempt_at: Optional final retry instant; usually ``None``.
            last_error: Safe short failure description.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Clears ``locked_at`` and updates the row by primary key.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.FAILED.value
        delivery.next_attempt_at = next_attempt_at
        delivery.locked_at = None
        delivery.last_error = last_error
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(ReminderDeliveryRow.id == delivery.id)
            .values(
                status=delivery.status,
                next_attempt_at=delivery.next_attempt_at,
                locked_at=delivery.locked_at,
                last_error=delivery.last_error,
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

    async def cancel_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Cancel one delivery without sending it.

        Purpose:
            Stop a claimed slot when the owning task, series, toggle, or project
            is no longer dispatchable.

        Args:
            delivery: Domain delivery to cancel.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Sets ``status='cancelled'``, clears the lease and retry slot, and
              updates the row by primary key.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.CANCELLED.value
        delivery.locked_at = None
        delivery.next_attempt_at = None
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(ReminderDeliveryRow.id == delivery.id)
            .values(
                status=delivery.status,
                locked_at=delivery.locked_at,
                next_attempt_at=delivery.next_attempt_at,
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

    async def reactivate_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Reset a cancelled delivery to pending.

        Purpose:
            Reuse the unique ``(reminder_id, scheduled_for)`` row when a slot
            becomes deliverable again.

        Args:
            delivery: Cancelled domain delivery to reactivate.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Resets attempts, locks, retry slots, and send metadata.
            - Updates the row by primary key.
            - Flushes but does not commit.
        """
        delivery.status = DeliveryStatus.PENDING.value
        delivery.attempts = 0
        delivery.locked_at = None
        delivery.next_attempt_at = None
        delivery.sent_at = None
        delivery.telegram_message_id = None
        delivery.last_error = None
        await self._session.execute(
            update(ReminderDeliveryRow)
            .where(ReminderDeliveryRow.id == delivery.id)
            .values(
                status=delivery.status,
                attempts=delivery.attempts,
                locked_at=delivery.locked_at,
                next_attempt_at=delivery.next_attempt_at,
                sent_at=delivery.sent_at,
                telegram_message_id=delivery.telegram_message_id,
                last_error=delivery.last_error,
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()


def _new_reminder_row(reminder: TaskReminder) -> TaskReminderRow:
    """
    Build a fresh ORM reminder row from a domain reminder.

    Purpose:
        Insert new reminders without copying the transient domain identifier,
        so PostgreSQL can generate the primary key.

    Args:
        reminder: Domain reminder to insert.

    Returns:
        TaskReminderRow: A transient ORM row populated with persisted fields.

    Notes:
        - The returned row is not added to a session and has no identifier.
    """
    return TaskReminderRow(
        user_id=reminder.user_id,
        task_id=reminder.task_id,
        series_id=reminder.series_id,
        kind=reminder.kind,
        enabled=reminder.enabled,
        offset_minutes=reminder.offset_minutes,
        interval_days=reminder.interval_days,
        time_of_day=reminder.time_of_day,
        timezone=reminder.timezone,
        next_run_at=reminder.next_run_at,
        created_at=reminder.created_at,
        updated_at=reminder.updated_at,
    )


def _new_delivery_row(delivery: ReminderDelivery) -> ReminderDeliveryRow:
    """
    Build a fresh ORM delivery row from a domain delivery.

    Purpose:
        Insert new deliveries without copying the transient domain identifier,
        so PostgreSQL can generate the primary key.

    Args:
        delivery: Domain delivery to insert.

    Returns:
        ReminderDeliveryRow: A transient ORM row populated with persisted
            fields.

    Notes:
        - The returned row is not added to a session and has no identifier.
    """
    return ReminderDeliveryRow(
        reminder_id=delivery.reminder_id,
        user_id=delivery.user_id,
        scheduled_for=delivery.scheduled_for,
        status=delivery.status,
        attempts=delivery.attempts,
        locked_at=delivery.locked_at,
        next_attempt_at=delivery.next_attempt_at,
        sent_at=delivery.sent_at,
        telegram_message_id=delivery.telegram_message_id,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
    )
