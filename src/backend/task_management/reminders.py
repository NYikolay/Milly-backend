from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from math import ceil
from time import monotonic as _monotonic_clock
from typing import Protocol
from zoneinfo import ZoneInfo

import structlog

from backend.core.errors import BadRequestError
from backend.models.reminder_delivery import ReminderDelivery
from backend.models.task_reminder import TaskReminder
from backend.task_management.enums import DeliveryStatus, Priority, ReminderKind
from backend.task_management.ports import TaskManagementRepository
from backend.task_management.schedule import (
    before_due_time,
    first_phase_scheduled_time,
    first_scheduled_time,
    localize_wall_time,
)

_INVALID_REMINDER = "Invalid reminder configuration"
_DUPLICATE_REMINDER = "Duplicate reminder kind"
_MAX_ATTEMPTS = 5
_LOCK_LEASE = timedelta(minutes=5)
_BACKOFF_BASE = timedelta(seconds=60)
_MESSAGE_HEADER = "⏰ Напоминание о задаче"
_DESCRIPTION_LIMIT = 200
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_PRIORITY_LINES = {
    Priority.URGENT_IMPORTANT.value: "🔴 Срочные и важные",
    Priority.IMPORTANT_NOT_URGENT.value: "🟡 Важные, но не срочные",
    Priority.URGENT_NOT_IMPORTANT.value: "🟠 Срочные, но не важные",
}
_MISSING_TELEGRAM = "Telegram profile is missing"
_DISPATCH_BATCH_SIZE = 100
_DISPATCH_TIMEOUT_SECONDS = 240.0

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
            text: Telegram HTML message text.

        Returns:
            int | None: Telegram message identifier when available.

        Raises:
            Exception: Implementation-specific delivery failures.
        """
        ...


@dataclass(frozen=True)
class ReminderContent:
    """Resolved task or series data used to render a reminder message."""

    title: str
    description: str | None
    priority: str
    due_at: datetime | None
    project_name: str | None


def build_reminder_text(
    content: ReminderContent,
    *,
    kind: ReminderKind,
    offset_minutes: int | None,
    now: datetime,
    timezone_name: str | None,
) -> str:
    """
    Render the Telegram HTML message for a due reminder.

    Purpose:
        Turn resolved task data into one readable card with an optional
        description, due information, priority, and project.

    Args:
        content: Resolved owner data for the reminder.
        kind: Reminder rule kind that produced the delivery.
        offset_minutes: Configured before-due offset when ``kind`` is
            ``ReminderKind.BEFORE_DUE``.
        now: Current instant used for relative day labels and countdowns.
        timezone_name: IANA timezone used to render the due instant. Falls back
            to UTC when empty.

    Returns:
        str: Telegram HTML message text with user data escaped.

    Notes:
        - Missing optional fields are omitted instead of leaving blank lines.
        - Long descriptions are truncated to the display limit with an ellipsis.
        - The countdown uses the actual time left when the due instant is known.
    """
    title_block = f"📝 <b>{escape(content.title, quote=False)}</b>"
    if content.description:
        title_block += f"\n{_format_description(content.description)}"

    meta: list[str] = []
    countdown = _countdown_minutes(content.due_at, now=now, offset_minutes=offset_minutes)
    if kind is ReminderKind.BEFORE_DUE:
        meta.append(
            "⏳ Дедлайн наступил"
            if countdown == 0
            else f"⏳ До дедлайна {_format_duration(countdown)}"
        )
    elif content.due_at is not None:
        meta.append(f"📅 {_format_due(content.due_at, now=now, timezone_name=timezone_name)}")
    priority_line = _PRIORITY_LINES.get(content.priority)
    if priority_line is not None:
        meta.append(priority_line)
    if content.project_name is not None:
        meta.append(f"📁 {escape(content.project_name, quote=False)}")

    if not meta:
        return f"{_MESSAGE_HEADER}\n\n{title_block}"
    return f"{_MESSAGE_HEADER}\n\n{title_block}\n\n" + "\n".join(meta)


def _format_description(description: str) -> str:
    """
    Escape and truncate a task description for the reminder message.

    Purpose:
        Keep the card compact and safe for Telegram HTML parsing.

    Args:
        description: Raw task or series description.

    Returns:
        str: Escaped description of at most ``_DESCRIPTION_LIMIT`` characters
            plus a trailing ellipsis when truncated.

    Notes:
        - The ellipsis is added after truncating the raw text, so escaped HTML
          entities never split in half.
    """
    text = description
    if len(text) > _DESCRIPTION_LIMIT:
        text = text[:_DESCRIPTION_LIMIT].rstrip() + "…"
    return escape(text, quote=False)


def _countdown_minutes(
    due_at: datetime | None,
    *,
    now: datetime,
    offset_minutes: int | None,
) -> int:
    """
    Return whole minutes left until the due instant.

    Purpose:
        Render an accurate before-due countdown even when delivery runs late.

    Args:
        due_at: Task due instant when known.
        now: Current instant used as the reference point.
        offset_minutes: Configured offset used when no due instant is known.

    Returns:
        int: Remaining whole minutes, rounded up, never below zero.

    Notes:
        - A configured offset is the fallback for series reminders without a
          due instant.
    """
    if due_at is not None:
        return max(0, ceil((due_at - now).total_seconds() / 60))
    return max(0, offset_minutes or 0)


def _format_duration(total_minutes: int) -> str:
    """
    Format a minute count as Russian days, hours, and minutes.

    Purpose:
        Keep the countdown natural for values from one minute to many days.

    Args:
        total_minutes: Positive number of minutes.

    Returns:
        str: Human-readable duration such as ``1 день 2 часа``.

    Notes:
        - Zero parts are omitted; only nonzero units are shown.
    """
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(_plural(days, "день", "дня", "дней"))
    if hours:
        parts.append(_plural(hours, "час", "часа", "часов"))
    if minutes:
        parts.append(_plural(minutes, "минута", "минуты", "минут"))
    return " ".join(parts)


def _plural(value: int, one: str, few: str, many: str) -> str:
    """
    Attach the correct Russian plural form to a number.

    Purpose:
        Render durations without awkward forms like ``2 день``.

    Args:
        value: Nonzero quantity.
        one: Form for 1, 21, 31, and similar.
        few: Form for 2-4, 22-24, and similar.
        many: Form for 0, 5-20, and similar.

    Returns:
        str: Quantity followed by the selected form.
    """
    if 11 <= value % 100 <= 14:
        form = many
    elif value % 10 == 1:
        form = one
    elif 2 <= value % 10 <= 4:
        form = few
    else:
        form = many
    return f"{value} {form}"


def _format_due(due_at: datetime, *, now: datetime, timezone_name: str | None) -> str:
    """
    Format a due instant as a relative Russian date and time.

    Purpose:
        Show ``Сегодня``/``Завтра`` when useful and a day with month otherwise.

    Args:
        due_at: Timezone-aware due instant.
        now: Current instant used for relative day labels.
        timezone_name: IANA timezone for rendering. Falls back to UTC when
            empty.

    Returns:
        str: Rendered date such as ``Завтра в 18:00``.

    Notes:
        - The year is appended only when it differs from the current year.
    """
    tzinfo = ZoneInfo(timezone_name) if timezone_name else UTC
    local_due = due_at.astimezone(tzinfo)
    local_now = now.astimezone(tzinfo)
    days_ahead = (local_due.date() - local_now.date()).days
    if days_ahead == 0:
        label = "Сегодня"
    elif days_ahead == 1:
        label = "Завтра"
    elif days_ahead == -1:
        label = "Вчера"
    else:
        label = f"{local_due.day} {_MONTHS[local_due.month - 1]}"
        if local_due.year != local_now.year:
            label = f"{label} {local_due.year}"
    return f"{label} в {local_due:%H:%M}"


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
    scheduled_starts_on: date | None = None,
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
        scheduled_starts_on: Phase date used to anchor a scheduled reminder
            instead of the due-relative rule. Defaults to ``None``.

    Returns:
        TaskReminder | None: The reminder row, or ``None`` when the before-due
            moment has passed.

    Raises:
        BadRequestError: If the reminder schedule overflows the datetime range.

    Notes:
        - A supplied ``scheduled_starts_on`` keeps scheduled slots on the
          series phase even when the reminder wall time is earlier than
          ``due_at``.
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
        if scheduled_starts_on is not None:
            slot = first_phase_scheduled_time(
                starts_on=scheduled_starts_on,
                time_of_day=reminder.time_of_day or time(0, 0),
                timezone=ZoneInfo(timezone_name),
                interval_days=reminder.interval_days or 1,
                now=now,
            )
        else:
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
        TaskReminder: The template reminder row.

    Notes:
        - Template reminders never carry ``next_run_at``; only generated task
          instances receive deliverable slots.
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

    Notes:
        - Stale slots are skipped in whole intervals using date arithmetic,
          leaving at most a couple of iterations for DST correction.
        - The returned slot is strictly after both ``current`` and ``now``.
    """
    if reminder.kind == ReminderKind.BEFORE_DUE.value:
        return None
    timezone = ZoneInfo(reminder.timezone or "UTC")
    time_of_day = reminder.time_of_day or time(0, 0)
    interval_days = reminder.interval_days or 1
    try:
        current_date = current.astimezone(timezone).date()
        local_now = now.astimezone(timezone)
        steps = max(0, (local_now.date() - current_date).days // interval_days)
        current_date += timedelta(days=steps * interval_days)
        slot = localize_wall_time(current_date, time_of_day, timezone)
        anchor = max(current, now)
        while slot <= anchor:
            current_date += timedelta(days=interval_days)
            slot = localize_wall_time(current_date, time_of_day, timezone)
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
        monotonic: Callable[[], float] = _monotonic_clock,
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
            monotonic: Monotonic time provider used for the dispatch deadline.
                Defaults to ``time.monotonic``.

        Notes:
            - The dispatcher commits only after claiming a delivery, then
              flushes the final delivery state.
        """
        self._repository = repository
        self._sender = sender
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._monotonic = monotonic

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
              reminder without touching expired ORM objects.
        """
        now = self._clock()
        started = self._monotonic()
        sent = 0
        candidates = [
            (reminder.id, reminder.user_id)
            for reminder in await self._repository.list_due_reminders(
                now,
                limit=_DISPATCH_BATCH_SIZE,
            )
        ]
        for reminder_id, _user_id in candidates:
            if self._monotonic() - started >= _DISPATCH_TIMEOUT_SECONDS:
                break
            try:
                reminder = await self._repository.claim_due_reminder(reminder_id, now)
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

                content = await self._dispatchable_content(reminder)
                if content is None:
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

                next_slot = next_reminder_slot(reminder, current=scheduled_for, now=now)
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
                        next_slot,
                    )
                    await self._repository.commit()
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
                    await self._repository.commit()
                    continue

                await self._repository.mark_delivery_sent(delivery, now, message_id)
                await self._repository.commit()
                await self._repository.update_reminder_next_run_at(reminder, next_slot)
                await self._repository.commit()
                sent += 1
            except Exception as error:
                await self._repository.rollback()
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
            task = await self._repository.get_task(reminder.user_id, reminder.task_id)
            if task is None or task.completed_at is not None or not task.reminders_enabled:
                return None
            if task.project_id is not None and not await self._repository.project_is_active(
                reminder.user_id,
                task.project_id,
            ):
                return None
            if task.series_id is not None:
                series = await self._repository.get_series(
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
            series = await self._repository.get_series(reminder.user_id, reminder.series_id)
            if series is None or not series.reminders_enabled:
                return None
            if series.project_id is not None and not await self._repository.project_is_active(
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
        project = await self._repository.get_project(user_id, project_id)
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
