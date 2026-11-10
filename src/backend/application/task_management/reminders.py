"""Reminder delivery orchestration for due task and series reminders."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic as _monotonic_clock

import structlog

from backend.application.ports.collaborators import (
    ProjectReader,
    ReminderSender,
    SeriesReader,
    TaskReader,
)
from backend.application.ports.task_management import (
    AccountRepository,
    ReminderRepository,
)
from backend.application.ports.unit_of_work import UnitOfWork
from backend.domain.entities import ReminderDelivery, TaskReminder
from backend.domain.enums import DeliveryStatus, ReminderKind
from backend.domain.task_management.models import ReminderContent
from backend.domain.task_management.policies import (
    build_reminder_text,
    next_reminder_slot,
)

_MAX_ATTEMPTS = 5
_LOCK_LEASE = timedelta(minutes=5)
_BACKOFF_BASE = timedelta(seconds=60)
_MISSING_TELEGRAM = "Telegram profile is missing"
_DISPATCH_BATCH_SIZE = 100
_DISPATCH_TIMEOUT_SECONDS = 240.0

logger = structlog.get_logger(__name__)


class ReminderDispatcher:
    """Delivers due reminder messages and tracks idempotent delivery rows."""

    def __init__(
        self,
        reminder_repository: ReminderRepository,
        task_reader: TaskReader,
        series_reader: SeriesReader,
        project_reader: ProjectReader,
        account_repository: AccountRepository,
        unit_of_work: UnitOfWork,
        sender: ReminderSender,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """
        Initialize the dispatcher with persistence, transport, and time ports.

        Purpose:
            Keep delivery orchestration independent of Taskiq and Telegram
            while allowing deterministic clocks in tests.

        Args:
            reminder_repository: Persistence operations for reminders and
                deliveries.
            task_reader: Task lookups used to recheck delivery eligibility.
            series_reader: Series lookups used to recheck reminder content.
            project_reader: Project activity and display-name lookups.
            account_repository: Telegram chat lookup for the reminder owner.
            unit_of_work: Transaction boundary for claims, delivery outcomes,
                and slot updates.
            sender: Telegram-like message sender.
            clock: Current-time provider. Defaults to the current UTC time.
            monotonic: Monotonic time provider used for the dispatch deadline.
                Defaults to ``time.monotonic``.

        Notes:
            - Repositories flush only; every explicit commit and rollback goes
              through ``unit_of_work``.
        """
        self._reminder_repository = reminder_repository
        self._task_reader = task_reader
        self._series_reader = series_reader
        self._project_reader = project_reader
        self._account_repository = account_repository
        self._unit_of_work = unit_of_work
        self._sender = sender
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._monotonic = monotonic if monotonic is not None else _monotonic_clock

    async def dispatch_due(self) -> int:
        """
        Deliver one bounded batch of due reminders and return the send count.

        Purpose:
            Run the periodic reminder-delivery job without duplicate sends or
            unbounded runtime.

        Returns:
            int: Number of deliveries successfully sent in this run.

        Notes:
            - At most ``_DISPATCH_BATCH_SIZE`` due reminders are listed per run.
            - The deadline is checked before each candidate; an in-flight send
              always completes and remaining due reminders stay for the next
              run once ``_DISPATCH_TIMEOUT_SECONDS`` has elapsed.
            - Already sent ``(reminder_id, scheduled_for)`` keys are skipped.
            - Cancelled deliveries for a current slot are reactivated.
            - Messages are rendered as HTML cards with escaped user data.
            - Each claimed reminder is rechecked for an active master toggle,
              non-deleted owner, and active project before sending.
            - A missing Telegram profile fails the delivery without sending.
            - Retryable failures stay pending with exponential backoff; the
              fifth failed attempt marks the delivery failed.
            - Before-due reminders clear ``next_run_at`` after a successful or
              final failed delivery.
            - The next slot is computed before sending; a successful send is
              committed before the slot update so a later slot failure cannot
              cause a second message.
            - Every delivery outcome commits so earlier work survives a later
              failure; unexpected errors roll back and continue with the next
              reminder without touching stale entities.
        """
        now = self._clock()
        started = self._monotonic()
        sent = 0
        candidates = [
            (reminder.id, reminder.user_id)
            for reminder in await self._reminder_repository.list_due_reminders(
                now,
                limit=_DISPATCH_BATCH_SIZE,
            )
        ]
        for reminder_id, _user_id in candidates:
            if self._monotonic() - started >= _DISPATCH_TIMEOUT_SECONDS:
                break
            try:
                reminder = await self._reminder_repository.claim_due_reminder(reminder_id, now)
                if reminder is None or reminder.next_run_at is None:
                    await self._unit_of_work.commit()
                    continue

                scheduled_for = reminder.next_run_at
                delivery = await self._reminder_repository.get_delivery(
                    reminder.id,
                    scheduled_for,
                )
                if delivery is not None and delivery.status == DeliveryStatus.SENT.value:
                    await self._reminder_repository.update_reminder_next_run_at(
                        reminder,
                        next_reminder_slot(reminder, current=scheduled_for, now=now),
                    )
                    await self._unit_of_work.commit()
                    continue

                content = await self._dispatchable_content(reminder)
                if content is None:
                    if delivery is None:
                        delivery = await self._reminder_repository.add_delivery(
                            _new_delivery(
                                reminder=reminder,
                                scheduled_for=scheduled_for,
                            )
                        )
                    if delivery.status != DeliveryStatus.CANCELLED.value:
                        await self._reminder_repository.cancel_delivery(delivery)
                    await self._reminder_repository.update_reminder_next_run_at(reminder, None)
                    await self._unit_of_work.commit()
                    continue

                if delivery is not None and delivery.status == DeliveryStatus.CANCELLED.value:
                    await self._reminder_repository.reactivate_delivery(delivery)
                if delivery is None:
                    delivery = await self._reminder_repository.add_delivery(
                        _new_delivery(
                            reminder=reminder,
                            scheduled_for=scheduled_for,
                        )
                    )
                if (
                    delivery.status == DeliveryStatus.SENDING.value
                    and delivery.locked_at is not None
                    and now - delivery.locked_at < _LOCK_LEASE
                ):
                    await self._unit_of_work.commit()
                    continue
                if (
                    delivery.status == DeliveryStatus.PENDING.value
                    and delivery.next_attempt_at is not None
                    and delivery.next_attempt_at > now
                ):
                    await self._unit_of_work.commit()
                    continue
                if (
                    delivery.status == DeliveryStatus.FAILED.value
                    and delivery.next_attempt_at is None
                ):
                    await self._reminder_repository.update_reminder_next_run_at(
                        reminder,
                        next_reminder_slot(reminder, current=scheduled_for, now=now),
                    )
                    await self._unit_of_work.commit()
                    continue

                next_slot = next_reminder_slot(reminder, current=scheduled_for, now=now)
                await self._reminder_repository.mark_delivery_sending(delivery, now)
                await self._unit_of_work.commit()
                chat_id = await self._account_repository.get_telegram_id(reminder.user_id)
                if chat_id is None:
                    await self._reminder_repository.mark_delivery_failed(
                        delivery,
                        None,
                        _MISSING_TELEGRAM,
                    )
                    await self._reminder_repository.update_reminder_next_run_at(
                        reminder,
                        next_slot,
                    )
                    await self._unit_of_work.commit()
                    continue
                try:
                    message_id = await self._sender.send(
                        chat_id,
                        build_reminder_text(
                            content,
                            kind=ReminderKind(reminder.kind),
                            offset_minutes=reminder.offset_minutes,
                            now=now,
                            timezone_name=reminder.timezone,
                        ),
                    )
                except Exception as error:
                    await self._record_failure(
                        reminder,
                        delivery,
                        scheduled_for=scheduled_for,
                        error=error,
                        now=now,
                    )
                    await self._unit_of_work.commit()
                    continue

                await self._reminder_repository.mark_delivery_sent(delivery, now, message_id)
                await self._unit_of_work.commit()
                await self._reminder_repository.update_reminder_next_run_at(reminder, next_slot)
                await self._unit_of_work.commit()
                sent += 1
            except Exception as error:
                await self._unit_of_work.rollback()
                logger.warning(
                    "task.reminder_dispatch_failed",
                    reminder_id=reminder_id,
                    error=type(error).__name__,
                )
        return sent

    async def _dispatchable_content(self, reminder: TaskReminder) -> ReminderContent | None:
        """
        Resolve the owner data when a claimed reminder may still be delivered.

        Purpose:
            Recheck the task or series, the parent series toggle, and the
            project after the row lock so a concurrent completion, toggle,
            archive, or deletion cannot send a stale reminder, then collect the
            fields used to render the message.

        Args:
            reminder: Reminder claimed for delivery.

        Returns:
            ReminderContent | None: Resolved owner data, or ``None`` when
                delivery must be cancelled.

        Notes:
            - The project name is loaded only after the active check passes.
            - Series content carries no due instant because template reminders
              have no schedule of their own.
        """
        if reminder.task_id is not None:
            task = await self._task_reader.get_task(reminder.user_id, reminder.task_id)
            if task is None or task.completed_at is not None or not task.reminders_enabled:
                return None
            if task.project_id is not None and not await self._project_reader.project_is_active(
                reminder.user_id,
                task.project_id,
            ):
                return None
            if task.series_id is not None:
                series = await self._series_reader.get_series(
                    reminder.user_id,
                    task.series_id,
                )
                if series is None or not series.reminders_enabled:
                    return None
            return ReminderContent(
                title=task.title,
                description=task.description,
                priority=task.priority,
                due_at=task.due_at,
                project_name=await self._project_name(reminder.user_id, task.project_id),
            )
        if reminder.series_id is not None:
            series = await self._series_reader.get_series(
                reminder.user_id,
                reminder.series_id,
            )
            if series is None or not series.reminders_enabled:
                return None
            if series.project_id is not None and not await self._project_reader.project_is_active(
                reminder.user_id,
                series.project_id,
            ):
                return None
            return ReminderContent(
                title=series.title,
                description=series.description,
                priority=series.priority,
                due_at=None,
                project_name=await self._project_name(reminder.user_id, series.project_id),
            )
        return None

    async def _project_name(self, user_id: int, project_id: int | None) -> str | None:
        """
        Load the project name used in a reminder message.

        Purpose:
            Show the project in the reminder card without failing delivery when
            the project disappeared between the active check and this read.

        Args:
            user_id: Owner identifier.
            project_id: Project reference from the task or series, when set.

        Returns:
            str | None: The project name, or ``None`` for reminders without a
                project or with a missing row.

        Notes:
            - Performs no writes and does not commit.
        """
        if project_id is None:
            return None
        project = await self._project_reader.get_project(user_id, project_id)
        return project.name if project is not None else None

    async def _record_failure(
        self,
        reminder: TaskReminder,
        delivery: ReminderDelivery,
        *,
        scheduled_for: datetime,
        error: Exception,
        now: datetime,
    ) -> None:
        """
        Record a sender failure with backoff or final failure.

        Purpose:
            Apply the retry policy after Telegram rejects or fails a send.

        Args:
            reminder: Reminder whose slot was being delivered.
            delivery: Delivery row being attempted.
            scheduled_for: Slot that failed.
            error: Raised sender exception.
            now: Current instant used for backoff and slot advancement.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Retryable failures keep the current reminder slot so the next
              run can retry after ``next_attempt_at``.
            - Final failures advance scheduled slots and clear before-due slots.
        """
        attempts = delivery.attempts
        error_name = type(error).__name__
        if attempts >= _MAX_ATTEMPTS:
            await self._reminder_repository.mark_delivery_failed(delivery, None, error_name)
            await self._reminder_repository.update_reminder_next_run_at(
                reminder,
                next_reminder_slot(reminder, current=scheduled_for, now=now),
            )
            return
        backoff = _BACKOFF_BASE * (2 ** (attempts - 1))
        await self._reminder_repository.mark_delivery_pending(
            delivery,
            now + backoff,
            error_name,
        )


def _new_delivery(
    *,
    reminder: TaskReminder,
    scheduled_for: datetime,
) -> ReminderDelivery:
    """
    Build a pending delivery row for a reminder slot.

    Purpose:
        Create the idempotent delivery record before claiming it.

    Args:
        reminder: Reminder being delivered.
        scheduled_for: Slot being delivered.

    Returns:
        ReminderDelivery: A pending delivery row.
    """
    return ReminderDelivery(
        reminder_id=reminder.id,
        user_id=reminder.user_id,
        scheduled_for=scheduled_for,
        status=DeliveryStatus.PENDING.value,
        attempts=0,
    )
