"""Pure date, schedule, and reminder policies for task management."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from math import ceil
from zoneinfo import ZoneInfo

from backend.core.errors import BadRequestError
from backend.domain.entities import TaskReminder
from backend.domain.enums import Priority, ReminderKind
from backend.domain.task_management.models import ReminderContent, ReminderInput

MIN_SUPPORTED_DATE: date = date(2025, 1, 1)
MAX_SUPPORTED_DATE: date = date(2100, 12, 31)
MIN_SUPPORTED_DATETIME: datetime = datetime(2025, 1, 1, tzinfo=UTC)
MAX_SUPPORTED_DATETIME: datetime = datetime(2100, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
HORIZON_DAYS: int = 90

_INVALID_REMINDER = "Invalid reminder configuration"
_DUPLICATE_REMINDER = "Duplicate reminder kind"
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


def require_supported_date(value: date) -> date:
    """
    Require a calendar date to fall inside the supported inclusive range.

    Purpose:
        Reject user dates that are too far in the past or future before they
        reach persistence or schedule arithmetic.

    Args:
        value: Candidate calendar date.

    Returns:
        date: The unchanged supported date.

    Raises:
        ValueError: If the date is before ``MIN_SUPPORTED_DATE`` or after
            ``MAX_SUPPORTED_DATE``.

    Notes:
        - Bounds are inclusive: both endpoints are accepted.
    """
    if value < MIN_SUPPORTED_DATE or value > MAX_SUPPORTED_DATE:
        raise ValueError("Date is out of the supported range")
    return value


def require_supported_datetime(value: datetime) -> datetime:
    """
    Require an instant to be timezone-aware and inside the supported UTC range.

    Purpose:
        Keep user due times unambiguous and bounded before schedule arithmetic
        can overflow or walk over a very large span.

    Args:
        value: Candidate absolute instant.

    Returns:
        datetime: The unchanged supported instant.

    Raises:
        ValueError: If the instant is naive, or if its UTC equivalent is before
            ``MIN_SUPPORTED_DATETIME`` or after ``MAX_SUPPORTED_DATETIME``.

    Notes:
        - Bounds are inclusive: both endpoints are accepted.
        - Comparison uses the UTC equivalent, so an offset can move a wall-clock
          value outside the range.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime must be timezone-aware")
    try:
        utc_value = value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("Datetime is out of the supported range") from error
    if utc_value < MIN_SUPPORTED_DATETIME or utc_value > MAX_SUPPORTED_DATETIME:
        raise ValueError("Datetime is out of the supported range")
    return value


def localize_wall_time(local_date: date, time_of_day: time, timezone: ZoneInfo) -> datetime:
    """
    Convert a local wall-clock date and time to an aware UTC datetime.

    Purpose:
        Resolve a wall-clock moment in an IANA timezone to an absolute instant,
        including daylight-saving transitions.

    Args:
        local_date: Calendar date in the target timezone.
        time_of_day: Naive local wall-clock time to resolve.
        timezone: IANA timezone used to interpret the wall-clock values.

    Returns:
        datetime: The resolved instant as an aware UTC datetime.

    Raises:
        ValueError: If ``time_of_day`` already carries timezone information.
        OverflowError: If the resolved instant falls outside the supported
            datetime range.

    Notes:
        - Ambiguous fall-back times use ``fold=0`` and therefore the first occurrence.
        - Nonexistent spring-forward times resolve to the first valid instant after
          the gap, discarding any minutes that fall inside the gap.
    """
    if time_of_day.tzinfo is not None:
        raise ValueError("time_of_day must be timezone-naive")

    naive = datetime.combine(local_date, time_of_day)
    candidate = naive.replace(tzinfo=timezone, fold=0)
    localized = candidate.astimezone(UTC).astimezone(timezone)
    if localized.replace(tzinfo=None) == naive:
        return candidate.astimezone(UTC)

    folded = naive.replace(tzinfo=timezone, fold=1)
    target_offset = folded.utcoffset()
    lower = int(folded.astimezone(UTC).timestamp())
    upper = int(candidate.astimezone(UTC).timestamp())
    while upper - lower > 1:
        middle = (lower + upper) // 2
        instant = datetime.fromtimestamp(middle, tz=UTC)
        if instant.astimezone(timezone).utcoffset() == target_offset:
            upper = middle
        else:
            lower = middle
    return datetime.fromtimestamp(upper, tz=UTC)


def _phase_on_or_after(anchor: date, target: date, interval_days: int) -> date:
    """
    Find the nearest anchor-aligned phase date at or after a target date.

    Purpose:
        Keep periodic scheduling aligned to the series anchor while jumping
        over any number of elapsed intervals in constant time.

    Args:
        anchor: First phase date of the series.
        target: Earliest local date the caller wants to consider.
        interval_days: Number of local days between phase dates.

    Returns:
        date: The anchor date when ``target`` is not later, otherwise the first
            anchor-aligned date at or after ``target``.

    Raises:
        OverflowError: If the aligned phase date exceeds the supported
            calendar range.

    Notes:
        - Date arithmetic uses whole calendar days, so daylight-saving changes
          never shift the phase.
    """
    if target <= anchor:
        return anchor
    steps = ((target - anchor).days + interval_days - 1) // interval_days
    return anchor + timedelta(days=steps * interval_days)


def series_due_times(
    *,
    starts_on: date,
    ends_on: date | None,
    time_of_day: time,
    timezone: ZoneInfo,
    now: datetime,
    interval_days: int = 1,
    generated_through: datetime | None = None,
    horizon_days: int = HORIZON_DAYS,
) -> list[datetime]:
    """
    Materialize phase-aligned series occurrences within the generation window.

    Purpose:
        Produce the missing due instants for a daily or periodic recurring
        task, never backfilling occurrences that are already in the past.

    Args:
        starts_on: First local date on which the series may occur and the phase
            anchor for periodic schedules.
        ends_on: Optional inclusive final local date of the series.
        time_of_day: Local wall-clock time of each occurrence.
        timezone: IANA timezone of the series.
        now: Current absolute instant used as the lower bound.
        interval_days: Number of local days between occurrences. Defaults to 1.
        generated_through: Instant through which occurrences were already generated.
        horizon_days: Number of days from ``now`` to materialize ahead.

    Returns:
        list[datetime]: Aware UTC due instants in ascending order.

    Raises:
        ValueError: If ``interval_days`` is less than one or ``time_of_day``
            already carries timezone information.
        OverflowError: If an occurrence falls outside the supported datetime
            range.

    Notes:
        - Phase dates are ``starts_on`` plus whole multiples of
          ``interval_days``; the phase is preserved across resumes and never
          drifts with daylight-saving transitions.
        - A first candidate beyond the horizon is returned alone only when no
          occurrences were generated yet, so a future series still receives its
          first occurrence.
        - When resuming from ``generated_through``, a candidate beyond the
          horizon yields an empty list instead of overshooting the window.
        - ``generated_through`` resumes on the next local day after the instant,
          then advances to the nearest phase date at or after it.
        - The search starts at the later of the series start and the current
          local date; one whole interval is applied when that slot has already
          passed.
        - Daylight-saving gaps clamp the wall time to the first valid instant
          after the gap, while ambiguous fall-back times use the first
          occurrence.
        - The returned list never contains an instant at or before ``now``.
    """
    if interval_days < 1:
        raise ValueError("interval_days must be at least 1")

    local_now = now.astimezone(timezone)
    if ends_on is not None and (ends_on < starts_on or ends_on < local_now.date()):
        return []

    lower_target = local_now.date()
    if generated_through is not None:
        generated_date = generated_through.astimezone(timezone).date()
        lower_target = generated_date + timedelta(days=1)

    candidate_date = _phase_on_or_after(
        starts_on,
        max(lower_target, local_now.date()),
        interval_days,
    )
    candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    if candidate <= now:
        candidate_date += timedelta(days=interval_days)
        candidate = localize_wall_time(candidate_date, time_of_day, timezone)

    if ends_on is not None and candidate_date > ends_on:
        return []

    horizon_end = now + timedelta(days=horizon_days)
    if candidate > horizon_end:
        return [candidate] if generated_through is None else []

    due_times: list[datetime] = []
    while candidate <= horizon_end and (ends_on is None or candidate_date <= ends_on):
        due_times.append(candidate)
        candidate_date += timedelta(days=interval_days)
        candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    return due_times


def next_occurrence_after_date(
    *,
    anchor: date,
    interval_days: int,
    time_of_day: time,
    timezone: ZoneInfo,
    now: datetime,
) -> datetime:
    """
    Compute the first phase occurrence strictly after the current local date.

    Purpose:
        Shift an overdue task or series forward to the next phase-aligned slot
        without losing the original schedule phase.

    Args:
        anchor: First phase date of the series.
        interval_days: Number of local days between occurrences.
        time_of_day: Local wall-clock time of each occurrence.
        timezone: IANA timezone of the series.
        now: Current absolute instant used to derive the local date.

    Returns:
        datetime: The next phase occurrence as an aware UTC datetime.

    Raises:
        ValueError: If ``interval_days`` is less than one or ``time_of_day``
            already carries timezone information.
        OverflowError: If the next occurrence falls outside the supported
            datetime range.

    Notes:
        - A future anchor is returned unchanged.
        - Elapsed intervals are skipped arithmetically, so the result always
          falls strictly after the current local date.
        - Phase dates use whole calendar days and therefore do not drift with
          daylight-saving transitions.
    """
    if interval_days < 1:
        raise ValueError("interval_days must be at least 1")

    local_today = now.astimezone(timezone).date()
    if anchor > local_today:
        occurrence_date = anchor
    else:
        steps = (local_today - anchor).days // interval_days + 1
        occurrence_date = anchor + timedelta(days=steps * interval_days)
    return localize_wall_time(occurrence_date, time_of_day, timezone)


def next_daily_time(*, current: datetime, time_of_day: time, timezone: ZoneInfo) -> datetime:
    """
    Advance a daily occurrence to the next local day.

    Purpose:
        Compute the following daily slot after an existing occurrence.

    Args:
        current: Existing absolute occurrence instant.
        time_of_day: Local wall-clock time of each occurrence.
        timezone: IANA timezone of the series.

    Returns:
        datetime: The next local day's occurrence as an aware UTC datetime.

    Raises:
        ValueError: If ``time_of_day`` already carries timezone information.
        OverflowError: If the next occurrence falls outside the supported
            datetime range.
    """
    local_date = current.astimezone(timezone).date() + timedelta(days=1)
    return localize_wall_time(local_date, time_of_day, timezone)


def before_due_time(*, due_at: datetime, offset_minutes: int) -> datetime:
    """
    Compute the reminder instant relative to a task due time.

    Purpose:
        Derive when a ``before_due`` reminder must fire.

    Args:
        due_at: Absolute task due instant.
        offset_minutes: Minutes to subtract from the due instant.

    Returns:
        datetime: The reminder instant with the same timezone awareness as ``due_at``.

    Raises:
        OverflowError: If subtracting the offset leaves the supported datetime
            range.
    """
    return due_at - timedelta(minutes=offset_minutes)


def first_scheduled_time(
    *,
    due_at: datetime,
    time_of_day: time,
    timezone: ZoneInfo,
    interval_days: int,
    now: datetime,
) -> datetime:
    """
    Compute the first scheduled reminder instant for a due task.

    Purpose:
        Find the first interval-anchored slot at or after the due moment that is
        still in the future.

    Args:
        due_at: Absolute task due instant.
        time_of_day: Local wall-clock time of scheduled reminders.
        timezone: IANA timezone used for the reminder schedule.
        interval_days: Number of local days between scheduled reminders.
        now: Current absolute instant used as the lower bound.

    Returns:
        datetime: The first scheduled reminder instant as an aware UTC datetime.

    Raises:
        ValueError: If ``interval_days`` is less than one or ``time_of_day``
            already carries timezone information.
        OverflowError: If a candidate falls outside the supported datetime
            range.

    Notes:
        - The first candidate is the first local ``time_of_day`` at or after
          ``due_at``; a future ``due_at`` therefore remains the first
          candidate.
        - Stale candidates are skipped in whole intervals using date arithmetic,
          leaving at most a couple of iterations for DST correction.
    """
    if interval_days < 1:
        raise ValueError("interval_days must be at least 1")

    candidate_date = due_at.astimezone(timezone).date()
    candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    if candidate < due_at:
        candidate_date += timedelta(days=1)
        candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    local_now = now.astimezone(timezone)
    steps = max(0, (local_now.date() - candidate_date).days // interval_days)
    candidate_date += timedelta(days=steps * interval_days)
    candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    while candidate <= now:
        candidate_date += timedelta(days=interval_days)
        candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    return candidate


def first_phase_scheduled_time(
    *,
    starts_on: date,
    time_of_day: time,
    timezone: ZoneInfo,
    interval_days: int,
    now: datetime,
) -> datetime:
    """
    Compute the first phase-anchored scheduled reminder instant after now.

    Purpose:
        Keep periodic scheduled reminder slots on series occurrence days when
        the reminder wall time is earlier than the task due time.

    Args:
        starts_on: A series phase date used as the reminder schedule anchor.
        time_of_day: Local wall-clock time of scheduled reminders.
        timezone: IANA timezone used for the reminder schedule.
        interval_days: Number of local days between phase occurrences.
        now: Current absolute instant used as the lower bound.

    Returns:
        datetime: The first phase occurrence at ``time_of_day`` strictly after
            ``now``, as an aware UTC datetime.

    Raises:
        ValueError: If ``interval_days`` is less than one or ``time_of_day``
            already carries timezone information.
        OverflowError: If a candidate falls outside the supported datetime
            range.

    Notes:
        - Phase dates are ``starts_on`` plus whole multiples of
          ``interval_days`` and never drift with daylight-saving transitions.
        - Unlike ``first_scheduled_time``, a slot earlier on the anchor date is
          valid and does not move the phase to the next day.
    """
    if interval_days < 1:
        raise ValueError("interval_days must be at least 1")

    local_now = now.astimezone(timezone)
    candidate_date = _phase_on_or_after(starts_on, local_now.date(), interval_days)
    candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    while candidate <= now:
        candidate_date += timedelta(days=interval_days)
        candidate = localize_wall_time(candidate_date, time_of_day, timezone)
    return candidate


def next_scheduled_time(
    *,
    current: datetime,
    time_of_day: time,
    timezone: ZoneInfo,
    interval_days: int,
) -> datetime:
    """
    Advance a scheduled reminder by one interval.

    Purpose:
        Compute the following scheduled reminder instant after an existing slot.

    Args:
        current: Existing absolute scheduled instant.
        time_of_day: Local wall-clock time of scheduled reminders.
        timezone: IANA timezone used for the reminder schedule.
        interval_days: Number of local days between scheduled reminders.

    Returns:
        datetime: The next scheduled instant as an aware UTC datetime.

    Raises:
        ValueError: If ``interval_days`` is less than one or ``time_of_day``
            already carries timezone information.
        OverflowError: If the next occurrence falls outside the supported
            datetime range.
    """
    if interval_days < 1:
        raise ValueError("interval_days must be at least 1")

    local_date = current.astimezone(timezone).date() + timedelta(days=interval_days)
    return localize_wall_time(local_date, time_of_day, timezone)


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
    Build a reminder entity for a task instance.

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
        TaskReminder | None: The reminder entity, or ``None`` when the
            before-due moment has passed.

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
    Build a template reminder entity for a series.

    Purpose:
        Store the reminder configuration copied into generated instances.

    Args:
        reminder: Validated reminder rule.
        user_id: Owner identifier.
        series_id: Series identifier.
        timezone_name: Series IANA timezone used for scheduled rules.

    Returns:
        TaskReminder: The template reminder entity.

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
    Recover a reminder rule from a persisted reminder entity.

    Purpose:
        Rebuild task reminders after a due-time change without losing their
        configured kind and parameters.

    Args:
        reminder: Persisted reminder entity.

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
