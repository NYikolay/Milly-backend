from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

HORIZON_DAYS: int = 90


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
        ValueError: If ``interval_days`` is less than one.

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
        ValueError: If ``interval_days`` is less than one.

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
        ValueError: If ``interval_days`` is less than one.

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
        ValueError: If ``interval_days`` is less than one.
    """
    if interval_days < 1:
        raise ValueError("interval_days must be at least 1")

    local_date = current.astimezone(timezone).date() + timedelta(days=interval_days)
    return localize_wall_time(local_date, time_of_day, timezone)
