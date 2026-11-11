"""Presentation helpers rendering reminder messages for delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from math import ceil
from zoneinfo import ZoneInfo

from backend.domain.enums import Priority, ReminderKind
from backend.domain.task_management.models import ReminderContent

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
