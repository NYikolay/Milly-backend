from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from backend.task_management.enums import (
    DeliveryStatus,
    Priority,
    ReminderKind,
    SeriesFrequency,
)
from backend.task_management.schedule import (
    before_due_time,
    daily_due_times,
    first_scheduled_time,
    localize_wall_time,
    next_daily_time,
    next_scheduled_time,
)

MOSCOW = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def test_daily_due_times_skips_past_and_starts_tomorrow() -> None:
    """Skip past slots and start on the next day when today's slot has passed."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result[0] == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)


def test_daily_due_times_uses_today_when_slot_is_ahead() -> None:
    """Keep today's slot when it is still ahead of the current instant."""
    now = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result[0] == datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def test_daily_due_times_includes_future_start_even_beyond_horizon() -> None:
    """Return the first slot when a future series starts beyond the horizon."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2027, 6, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == [datetime(2027, 6, 1, 6, 0, tzinfo=UTC)]


def test_daily_due_times_stops_at_ends_on_inclusive() -> None:
    """Include the final local day and stop after it."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 5),
        ends_on=date(2026, 11, 7),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == [
        datetime(2026, 11, 5, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 6, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
    ]


def test_daily_due_times_spring_forward_clamps_to_gap_end() -> None:
    """Clamp a nonexistent spring-forward wall time to the end of the gap."""
    now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 3, 29),
        ends_on=date(2026, 3, 29),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
    )
    assert result == [datetime(2026, 3, 29, 1, 0, tzinfo=UTC)]


def test_daily_due_times_fall_back_uses_first_occurrence() -> None:
    """Choose the first occurrence of an ambiguous fall-back wall time."""
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 10, 25),
        ends_on=date(2026, 10, 25),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
    )
    assert result == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]


def test_daily_due_times_resumes_after_generated_through() -> None:
    """Resume on the local day after the previously generated instant."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    generated_through = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=date(2026, 11, 8),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        generated_through=generated_through,
    )
    assert result == [
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 8, 6, 0, tzinfo=UTC),
    ]


def test_daily_due_times_generated_through_beyond_horizon_returns_one_candidate() -> None:
    """Return one candidate when generation already extends beyond the horizon."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    generated_through = datetime(2027, 6, 1, 6, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        generated_through=generated_through,
    )
    assert result == [datetime(2027, 6, 2, 6, 0, tzinfo=UTC)]


def test_daily_due_times_respects_custom_horizon() -> None:
    """Emit slots only through the configured horizon, inclusive."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        horizon_days=3,
    )
    assert result == [
        datetime(2026, 11, 5, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 6, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
    ]


def test_daily_due_times_returns_empty_when_ends_before_start() -> None:
    """Return no slots when the series end precedes its start."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 11, 7),
        ends_on=date(2026, 11, 5),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == []


def test_daily_due_times_returns_empty_when_ends_on_already_passed() -> None:
    """Return no slots when the series end precedes the current local date."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2026, 10, 1),
        ends_on=date(2026, 11, 3),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == []


def test_localize_wall_time_spring_forward_clamps_to_gap_end() -> None:
    """Clamp a nonexistent wall time to the first valid instant after the gap."""
    assert localize_wall_time(date(2026, 3, 29), time(2, 30), BERLIN) == datetime(
        2026, 3, 29, 1, 0, tzinfo=UTC
    )


def test_localize_wall_time_rejects_aware_time_of_day() -> None:
    """Reject a time_of_day that already carries timezone information."""
    with pytest.raises(ValueError, match="timezone-naive"):
        localize_wall_time(date(2026, 11, 4), time(9, 0, tzinfo=UTC), MOSCOW)


def test_localize_wall_time_fall_back_uses_fold_zero() -> None:
    """Resolve an ambiguous wall time to its first occurrence."""
    assert localize_wall_time(date(2026, 10, 25), time(2, 30), BERLIN) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )


def test_next_daily_time_advances_one_local_day() -> None:
    """Advance to the same wall-clock time on the following local day."""
    current = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
    assert next_daily_time(
        current=current,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
    ) == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)


def test_before_due_time_subtracts_offset() -> None:
    """Compute the reminder instant before the due time."""
    due_at = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert before_due_time(due_at=due_at, offset_minutes=30) == datetime(
        2026, 11, 4, 11, 30, tzinfo=UTC
    )


def test_first_scheduled_time_uses_next_local_slot_after_due() -> None:
    """Start scheduled reminders at the first local slot after the due moment."""
    due_at = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    now = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    result = first_scheduled_time(
        due_at=due_at,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=1,
        now=now,
    )
    assert result == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)


def test_first_scheduled_time_keeps_slot_on_due_date_when_ahead() -> None:
    """Keep the due date's slot when the wall time is later than the due moment."""
    due_at = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)
    now = datetime(2026, 11, 4, 1, 0, tzinfo=UTC)
    result = first_scheduled_time(
        due_at=due_at,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=2,
        now=now,
    )
    assert result == datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def test_first_scheduled_time_respects_interval() -> None:
    """Advance from the first slot after due by whole intervals until after now."""
    due_at = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    now = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    result = first_scheduled_time(
        due_at=due_at,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=3,
        now=now,
    )
    assert result == datetime(2026, 11, 11, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("interval_days", [0, -1])
def test_first_scheduled_time_rejects_non_positive_interval(interval_days: int) -> None:
    """Reject a zero or negative interval before searching for a slot."""
    with pytest.raises(ValueError, match="at least 1"):
        first_scheduled_time(
            due_at=datetime(2026, 11, 4, 15, 0, tzinfo=UTC),
            time_of_day=time(9, 0),
            timezone=MOSCOW,
            interval_days=interval_days,
            now=datetime(2026, 11, 4, 10, 0, tzinfo=UTC),
        )


def test_next_scheduled_time_advances_by_interval() -> None:
    """Advance to the next interval slot at the configured wall time."""
    current = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    assert next_scheduled_time(
        current=current,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        interval_days=2,
    ) == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("interval_days", [0, -1])
def test_next_scheduled_time_rejects_non_positive_interval(interval_days: int) -> None:
    """Reject a zero or negative interval before advancing a slot."""
    with pytest.raises(ValueError, match="at least 1"):
        next_scheduled_time(
            current=datetime(2026, 11, 5, 6, 0, tzinfo=UTC),
            time_of_day=time(9, 0),
            timezone=MOSCOW,
            interval_days=interval_days,
        )


def test_enum_values_match_database_contract() -> None:
    """Keep enum string values aligned with the database CHECK constraints."""
    assert {item.value for item in Priority} == {
        "urgent_important",
        "important_not_urgent",
        "urgent_not_important",
        "neither",
    }
    assert SeriesFrequency.DAILY.value == "daily"
    assert {item.value for item in ReminderKind} == {"before_due", "scheduled"}
    assert {item.value for item in DeliveryStatus} == {
        "pending",
        "sending",
        "sent",
        "failed",
        "cancelled",
    }
