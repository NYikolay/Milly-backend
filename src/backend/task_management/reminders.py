from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from backend.core.errors import BadRequestError
from backend.models.reminder_delivery import ReminderDelivery
from backend.models.task_reminder import TaskReminder
from backend.task_management.enums import DeliveryStatus, ReminderKind
from backend.task_management.ports import TaskManagementRepository
from backend.task_management.schedule import (
    before_due_time,
    first_scheduled_time,
    next_scheduled_time,
)

_INVALID_REMINDER = "Invalid reminder configuration"
_DUPLICATE_REMINDER = "Duplicate reminder kind"
_MAX_ATTEMPTS = 5
_LOCK_LEASE = timedelta(minutes=5)
_BACKOFF_BASE = timedelta(seconds=60)
_SENDER_TEXT = "Напоминание: {title}"
_MISSING_TELEGRAM = "Telegram profile is missing"

logger = structlog.get_logger(__name__)


class ReminderSender(Protocol):
    """Contract for delivering a reminder message to a Telegram chat."""

    async def send(self, chat_id: int, text: str) -> int | None:
        """
        Send a reminder message.

        Purpose:
            Keep the dispatcher independent of the Telegram transport.

        Args:
            chat_id: Telegram chat identifier from the user's profile.
            text: Message text.

        Returns:
            int | None: Telegram message identifier when available.

        Raises:
            Exception: Implementation-specific delivery failures.
        """
        ...


@dataclass(frozen=True)
class ReminderInput:
    """Validated reminder rule supplied when creating or updating tasks."""

    kind: ReminderKind
    enabled: bool = True
    offset_minutes: int | None = None
    interval_days: int | None = None
    time_of_day: time | None = None


def validate_reminder_inputs(
    reminders: Sequence[ReminderInput],
) -> tuple[ReminderInput, ...]:
    """
    Validate reminder rules and reject duplicate kinds.

    Purpose:
        Apply the same reminder contract as the database checks before any
        write reaches the repository.

    Args:
        reminders: Reminder rules from the caller.

    Returns:
        tuple[ReminderInput, ...]: The validated rules in input order.

    Raises:
        BadRequestError: If a rule is incomplete, inconsistent, duplicated, or
            carries a timezone-aware ``time_of_day``.
    """
    seen: set[ReminderKind] = set()
    for reminder in reminders:
        if reminder.kind in seen:
            raise BadRequestError(_DUPLICATE_REMINDER)
        seen.add(reminder.kind)
        if reminder.time_of_day is not None and (
            reminder.time_of_day.tzinfo is not None or reminder.time_of_day.utcoffset() is not None
        ):
            raise BadRequestError(_INVALID_REMINDER)
        if reminder.kind is ReminderKind.BEFORE_DUE:
            if reminder.offset_minutes is None or reminder.offset_minutes < 0:
                raise BadRequestError(_INVALID_REMINDER)
            if reminder.interval_days is not None or reminder.time_of_day is not None:
                raise BadRequestError(_INVALID_REMINDER)
        elif reminder.kind is ReminderKind.SCHEDULED:
            if reminder.interval_days is None or reminder.interval_days < 1:
                raise BadRequestError(_INVALID_REMINDER)
            if reminder.time_of_day is None or reminder.offset_minutes is not None:
                raise BadRequestError(_INVALID_REMINDER)
        else:
            raise BadRequestError(_INVALID_REMINDER)
    return tuple(reminders)


def build_task_reminder(
    reminder: ReminderInput,
    *,
    user_id: int,
    task_id: int,
    due_at: datetime,
    timezone_name: str,
    now: datetime,
) -> TaskReminder | None:
    """
    Build a reminder row for a task instance.

    Purpose:
        Compute the first deliverable slot and skip a before-due moment that is
        already in the past.

    Args:
        reminder: Validated reminder rule.
        user_id: Owner identifier.
        task_id: Task identifier.
        due_at: Task due instant.
        timezone_name: User IANA timezone used for scheduled rules.
        now: Current instant used as the lower bound.

    Returns:
        TaskReminder | None: The reminder row, or ``None`` when the before-due
            moment has passed.

    Raises:
        BadRequestError: If the reminder schedule overflows the datetime range.
    """
    if reminder.kind is ReminderKind.BEFORE_DUE:
        try:
            slot = before_due_time(
                due_at=due_at,
                offset_minutes=reminder.offset_minutes or 0,
            )
        except OverflowError as error:
            raise BadRequestError(_INVALID_REMINDER) from error
        if slot <= now:
            return None
        return TaskReminder(
            user_id=user_id,
            task_id=task_id,
            kind=reminder.kind.value,
            enabled=reminder.enabled,
            offset_minutes=reminder.offset_minutes,
            interval_days=None,
            time_of_day=None,
            timezone=None,
            next_run_at=slot,
        )

    try:
        slot = first_scheduled_time(
            due_at=due_at,
            time_of_day=reminder.time_of_day or time(0, 0),
            timezone=ZoneInfo(timezone_name),
            interval_days=reminder.interval_days or 1,
            now=now,
        )
    except OverflowError as error:
        raise BadRequestError(_INVALID_REMINDER) from error
    return TaskReminder(
        user_id=user_id,
        task_id=task_id,
        kind=reminder.kind.value,
        enabled=reminder.enabled,
        offset_minutes=None,
        interval_days=reminder.interval_days,
        time_of_day=reminder.time_of_day,
        timezone=timezone_name,
        next_run_at=slot,
    )


def build_series_reminder(
    reminder: ReminderInput,
    *,
    user_id: int,
    series_id: int,
    timezone_name: str,
) -> TaskReminder:
    """
    Build a template reminder row for a series.

    Purpose:
        Store the reminder configuration copied into generated instances.

    Args:
        reminder: Validated reminder rule.
        user_id: Owner identifier.
        series_id: Series identifier.
        timezone_name: Series IANA timezone used for scheduled rules.

    Returns:
        TaskReminder: A template reminder with ``next_run_at=None``.
    """
    return TaskReminder(
        user_id=user_id,
        series_id=series_id,
        kind=reminder.kind.value,
        enabled=reminder.enabled,
        offset_minutes=reminder.offset_minutes,
        interval_days=reminder.interval_days,
        time_of_day=reminder.time_of_day,
        timezone=timezone_name if reminder.kind is ReminderKind.SCHEDULED else None,
        next_run_at=None,
    )


def reminder_input_from_row(reminder: TaskReminder) -> ReminderInput:
    """
    Recover a reminder rule from a persisted row.

    Purpose:
        Rebuild task reminders after a due-time change without losing their
        configured kind and parameters.

    Args:
        reminder: Persisted reminder row.

    Returns:
        ReminderInput: The equivalent input rule.
    """
    return ReminderInput(
        kind=ReminderKind(reminder.kind),
        enabled=reminder.enabled,
        offset_minutes=reminder.offset_minutes,
        interval_days=reminder.interval_days,
        time_of_day=reminder.time_of_day,
    )


def next_reminder_slot(
    reminder: TaskReminder,
    *,
    current: datetime,
    now: datetime,
) -> datetime | None:
    """
    Advance a reminder to its next future slot.

    Purpose:
        Move scheduled reminders forward after a delivery or final failure.

    Args:
        reminder: Reminder to advance.
        current: Slot that just completed.
        now: Current instant used to skip stale slots.

    Returns:
        datetime | None: The next future slot, or ``None`` for before-due rules.

    Raises:
        BadRequestError: If advancing the schedule overflows the datetime range.
    """
    if reminder.kind == ReminderKind.BEFORE_DUE.value:
        return None
    timezone = ZoneInfo(reminder.timezone or "UTC")
    try:
        slot = next_scheduled_time(
            current=current,
            time_of_day=reminder.time_of_day or time(0, 0),
            timezone=timezone,
            interval_days=reminder.interval_days or 1,
        )
        while slot <= now:
            slot = next_scheduled_time(
                current=slot,
                time_of_day=reminder.time_of_day or time(0, 0),
                timezone=timezone,
                interval_days=reminder.interval_days or 1,
            )
    except OverflowError as error:
        raise BadRequestError(_INVALID_REMINDER) from error
    return slot


def rebuild_reminder_slot(
    reminder: TaskReminder,
    *,
    due_at: datetime,
    now: datetime,
) -> datetime | None:
    """
    Recompute a task reminder slot from the current time.

    Purpose:
        Restore reminders when a completed task is uncompleted.

    Args:
        reminder: Persisted task reminder.
        due_at: Task due instant.
        now: Current instant used as the lower bound.

    Returns:
        datetime | None: The next deliverable slot, or ``None`` when a
            before-due moment has passed.

    Raises:
        BadRequestError: If the reminder schedule overflows the datetime range.
    """
    if reminder.kind == ReminderKind.BEFORE_DUE.value:
        try:
            slot = before_due_time(
                due_at=due_at,
                offset_minutes=reminder.offset_minutes or 0,
            )
        except OverflowError as error:
            raise BadRequestError(_INVALID_REMINDER) from error
        return slot if slot > now else None
    try:
        return first_scheduled_time(
            due_at=due_at,
            time_of_day=reminder.time_of_day or time(0, 0),
            timezone=ZoneInfo(reminder.timezone or "UTC"),
            interval_days=reminder.interval_days or 1,
            now=now,
        )
    except OverflowError as error:
        raise BadRequestError(_INVALID_REMINDER) from error


class ReminderDispatcher:
    """Delivers due reminder messages and tracks idempotent delivery rows."""

    def __init__(
        self,
        repository: TaskManagementRepository,
        sender: ReminderSender,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the dispatcher with persistence, transport, and time ports.

        Purpose:
            Keep delivery orchestration independent of Taskiq and Telegram
            while allowing deterministic clocks in tests.

        Args:
            repository: Persistence operations for reminders and deliveries.
            sender: Telegram-like message sender.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The dispatcher commits only after claiming a delivery, then
              flushes the final delivery state.
        """
        self._repository = repository
        self._sender = sender
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def dispatch_due(self) -> int:
        """
        Deliver every due reminder and return the successful send count.

        Purpose:
            Run the periodic reminder-delivery job without duplicate sends.

        Returns:
            int: Number of deliveries successfully sent in this run.

        Notes:
            - Already sent ``(reminder_id, scheduled_for)`` keys are skipped.
            - Cancelled deliveries for a current slot are reactivated.
            - Each claimed reminder is rechecked for an active master toggle,
              non-deleted owner, and active project before sending.
            - A missing Telegram profile fails the delivery without sending.
            - Retryable failures stay pending with exponential backoff; the
              fifth failed attempt marks the delivery failed.
            - Before-due reminders clear ``next_run_at`` after a successful or
              final failed delivery.
            - Every delivery outcome commits so earlier work survives a later
              failure; unexpected errors roll back and continue with the next
              reminder.
        """
        now = self._clock()
        sent = 0
        for candidate in await self._repository.list_due_reminders(now):
            try:
                reminder = await self._repository.claim_due_reminder(candidate.id, now)
                if reminder is None or reminder.next_run_at is None:
                    await self._repository.commit()
                    continue

                scheduled_for = reminder.next_run_at
                delivery = await self._repository.get_delivery(reminder.id, scheduled_for)
                if delivery is not None and delivery.status == DeliveryStatus.SENT.value:
                    await self._repository.update_reminder_next_run_at(
                        reminder,
                        next_reminder_slot(reminder, current=scheduled_for, now=now),
                    )
                    await self._repository.commit()
                    continue

                title = await self._dispatchable_title(reminder)
                if title is None:
                    if delivery is None:
                        delivery = await self._repository.add_delivery(
                            _new_delivery(
                                reminder=reminder,
                                scheduled_for=scheduled_for,
                            )
                        )
                    if delivery.status != DeliveryStatus.CANCELLED.value:
                        await self._repository.cancel_delivery(delivery)
                    await self._repository.update_reminder_next_run_at(reminder, None)
                    await self._repository.commit()
                    continue

                if delivery is not None and delivery.status == DeliveryStatus.CANCELLED.value:
                    await self._repository.reactivate_delivery(delivery)
                if delivery is None:
                    delivery = await self._repository.add_delivery(
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
                    await self._repository.commit()
                    continue
                if (
                    delivery.status == DeliveryStatus.PENDING.value
                    and delivery.next_attempt_at is not None
                    and delivery.next_attempt_at > now
                ):
                    await self._repository.commit()
                    continue
                if (
                    delivery.status == DeliveryStatus.FAILED.value
                    and delivery.next_attempt_at is None
                ):
                    await self._repository.update_reminder_next_run_at(
                        reminder,
                        next_reminder_slot(reminder, current=scheduled_for, now=now),
                    )
                    await self._repository.commit()
                    continue

                await self._repository.mark_delivery_sending(delivery, now)
                await self._repository.commit()
                chat_id = await self._repository.get_telegram_id(reminder.user_id)
                if chat_id is None:
                    await self._repository.mark_delivery_failed(
                        delivery,
                        None,
                        _MISSING_TELEGRAM,
                    )
                    await self._repository.update_reminder_next_run_at(
                        reminder,
                        next_reminder_slot(reminder, current=scheduled_for, now=now),
                    )
                    await self._repository.commit()
                    continue
                try:
                    message_id = await self._sender.send(
                        chat_id,
                        _SENDER_TEXT.format(title=title),
                    )
                except Exception as error:
                    await self._record_failure(
                        reminder,
                        delivery,
                        scheduled_for=scheduled_for,
                        error=error,
                        now=now,
                    )
                    await self._repository.commit()
                    continue

                await self._repository.mark_delivery_sent(delivery, now, message_id)
                await self._repository.update_reminder_next_run_at(
                    reminder,
                    next_reminder_slot(reminder, current=scheduled_for, now=now),
                )
                await self._repository.commit()
                sent += 1
            except Exception as error:
                await self._repository.rollback()
                logger.warning(
                    "task.reminder_dispatch_failed",
                    reminder_id=candidate.id,
                    error=type(error).__name__,
                )
        return sent

    async def _dispatchable_title(self, reminder: TaskReminder) -> str | None:
        """
        Resolve the owner title when a claimed reminder may still be delivered.

        Purpose:
            Recheck the task or series and its project after the row lock so a
            concurrent completion, toggle, archive, or deletion cannot send a
            stale reminder.

        Args:
            reminder: Reminder claimed for delivery.

        Returns:
            str | None: The owner title, or ``None`` when delivery must be
                cancelled.
        """
        if reminder.task_id is not None:
            task = await self._repository.get_task(reminder.user_id, reminder.task_id)
            if task is None or task.completed_at is not None or not task.reminders_enabled:
                return None
            if task.project_id is not None and not await self._repository.project_is_active(
                reminder.user_id,
                task.project_id,
            ):
                return None
            return task.title
        if reminder.series_id is not None:
            series = await self._repository.get_series(reminder.user_id, reminder.series_id)
            if series is None or not series.reminders_enabled:
                return None
            if series.project_id is not None and not await self._repository.project_is_active(
                reminder.user_id,
                series.project_id,
            ):
                return None
            return series.title
        return None

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
            await self._repository.mark_delivery_failed(delivery, None, error_name)
            await self._repository.update_reminder_next_run_at(
                reminder,
                next_reminder_slot(reminder, current=scheduled_for, now=now),
            )
            return
        backoff = _BACKOFF_BASE * (2 ** (attempts - 1))
        await self._repository.mark_delivery_pending(
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
