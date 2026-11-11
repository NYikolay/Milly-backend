from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from backend.domain.enums import (
    DeliveryStatus,
    Priority,
    ReminderKind,
    SeriesFrequency,
    TaskRecurrence,
)
from backend.domain.task_management.policies import (
    before_due_time,
    first_scheduled_time,
    localize_wall_time,
    next_daily_time,
    next_occurrence_after_date,
    next_scheduled_time,
    series_due_times,
)

MOSCOW = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def test_series_due_times_skips_past_and_starts_tomorrow() -> None:
    """Skip past slots and start on the next day when today's slot has passed."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result[0] == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)


def test_series_due_times_uses_today_when_slot_is_ahead() -> None:
    """Keep today's slot when it is still ahead of the current instant."""
    now = datetime(2026, 11, 4, 4, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result[0] == datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def test_series_due_times_includes_future_start_even_beyond_horizon() -> None:
    """Return the first slot when a future series starts beyond the horizon."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2027, 6, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == [datetime(2027, 6, 1, 6, 0, tzinfo=UTC)]


def test_series_due_times_stops_at_ends_on_inclusive() -> None:
    """Include the final local day and stop after it."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
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


def test_series_due_times_spring_forward_clamps_to_gap_end() -> None:
    """Clamp a nonexistent spring-forward wall time to the end of the gap."""
    now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 3, 29),
        ends_on=date(2026, 3, 29),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
    )
    assert result == [datetime(2026, 3, 29, 1, 0, tzinfo=UTC)]


def test_series_due_times_fall_back_uses_first_occurrence() -> None:
    """Choose the first occurrence of an ambiguous fall-back wall time."""
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 10, 25),
        ends_on=date(2026, 10, 25),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
    )
    assert result == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]


def test_series_due_times_resumes_after_generated_through() -> None:
    """Resume on the local day after the previously generated instant."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    generated_through = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    result = series_due_times(
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


def test_series_due_times_generated_through_beyond_horizon_returns_empty() -> None:
    """Resuming past the horizon does not overshoot the generation window."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    generated_through = datetime(2027, 6, 1, 6, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        generated_through=generated_through,
    )
    assert result == []


def test_series_due_times_respects_custom_horizon() -> None:
    """Emit slots only through the configured horizon, inclusive."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
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


def test_series_due_times_returns_empty_when_ends_before_start() -> None:
    """Return no slots when the series end precedes its start."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 7),
        ends_on=date(2026, 11, 5),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == []


def test_series_due_times_returns_empty_when_ends_on_already_passed() -> None:
    """Return no slots when the series end precedes the current local date."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 10, 1),
        ends_on=date(2026, 11, 3),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    )
    assert result == []


def test_series_due_times_periodic_uses_phase_and_skips_passed_slot() -> None:
    """Start from the anchor phase and skip an already passed slot."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
    )
    assert result[0] == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    assert result[1] == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


def test_series_due_times_periodic_resume_keeps_anchor_phase() -> None:
    """Resume periodic generation on the anchor phase after generated_through."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=date(2026, 11, 16),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
        generated_through=datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
    )
    assert result == [
        datetime(2026, 11, 10, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 13, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 16, 6, 0, tzinfo=UTC),
    ]


def test_series_due_times_periodic_future_start_beyond_horizon_returns_one() -> None:
    """Return only the first phase slot when a future series starts beyond the horizon."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2027, 6, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
    )
    assert result == [datetime(2027, 6, 1, 6, 0, tzinfo=UTC)]


def test_series_due_times_periodic_stops_at_ends_on_inclusive() -> None:
    """Include the final phase date when it equals ends_on and stop after it."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=date(2026, 11, 10),
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
    )
    assert result == [
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 10, 6, 0, tzinfo=UTC),
    ]


def test_series_due_times_periodic_respects_horizon() -> None:
    """Stop emitting periodic slots once the horizon is exceeded."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 11, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
        interval_days=3,
        horizon_days=7,
    )
    assert result == [
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
        datetime(2026, 11, 10, 6, 0, tzinfo=UTC),
    ]


def test_series_due_times_periodic_spring_forward_clamps() -> None:
    """Clamp a periodic slot that falls into the spring-forward gap."""
    now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2026, 3, 29),
        ends_on=date(2026, 3, 29),
        time_of_day=time(2, 30),
        timezone=BERLIN,
        now=now,
        interval_days=1,
    )
    assert result == [datetime(2026, 3, 29, 1, 0, tzinfo=UTC)]


@pytest.mark.parametrize("interval_days", [0, -1])
def test_periodic_helpers_reject_non_positive_interval(interval_days: int) -> None:
    """Reject a zero or negative interval before materializing slots."""
    with pytest.raises(ValueError):
        series_due_times(
            starts_on=date(2026, 11, 1),
            ends_on=None,
            time_of_day=time(9, 0),
            timezone=MOSCOW,
            now=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
            interval_days=interval_days,
        )


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


def test_next_occurrence_after_date_is_strictly_after_today() -> None:
    """Return the next phase date strictly after the current local date."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert next_occurrence_after_date(
        anchor=date(2026, 11, 4),
        interval_days=1,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    ) == datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    assert next_occurrence_after_date(
        anchor=date(2026, 11, 1),
        interval_days=3,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    ) == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


def test_next_occurrence_after_date_periodic_skips_multiple_passed_slots() -> None:
    """Jump over several elapsed phase slots while preserving the anchor phase."""
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    assert next_occurrence_after_date(
        anchor=date(2026, 10, 20),
        interval_days=3,
        time_of_day=time(9, 0),
        timezone=MOSCOW,
        now=now,
    ) == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("interval_days", [0, -1])
def test_next_occurrence_after_date_rejects_non_positive_interval(interval_days: int) -> None:
    """Reject a zero or negative interval before computing the next phase date."""
    with pytest.raises(ValueError, match="at least 1"):
        next_occurrence_after_date(
            anchor=date(2026, 11, 1),
            interval_days=interval_days,
            time_of_day=time(9, 0),
            timezone=MOSCOW,
            now=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        )


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


def test_series_due_times_jumps_over_a_century() -> None:
    """Start from the current local date instead of walking from the series start."""
    now = datetime(2099, 12, 1, tzinfo=UTC)
    result = series_due_times(
        starts_on=date(2025, 1, 1),
        ends_on=None,
        time_of_day=time(9, 0),
        timezone=ZoneInfo("UTC"),
        now=now,
        generated_through=None,
        horizon_days=90,
    )
    assert result
    assert all(item > now for item in result)


def test_first_scheduled_time_jumps_over_many_intervals() -> None:
    """Skip stale intervals arithmetically instead of advancing one by one."""
    result = first_scheduled_time(
        due_at=datetime(2025, 1, 1, tzinfo=UTC),
        time_of_day=time(9, 0),
        timezone=ZoneInfo("UTC"),
        interval_days=7,
        now=datetime(2100, 12, 1, tzinfo=UTC),
    )
    assert result > datetime(2100, 12, 1, tzinfo=UTC)


def test_first_scheduled_time_keeps_future_due_anchor() -> None:
    """Keep a future due instant as the first candidate after the jump."""
    result = first_scheduled_time(
        due_at=datetime(2099, 12, 1, 10, 0, tzinfo=UTC),
        time_of_day=time(9, 0),
        timezone=ZoneInfo("UTC"),
        interval_days=7,
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert result == datetime(2099, 12, 2, 9, 0, tzinfo=UTC)


def test_task_recurrence_values() -> None:
    """Keep task recurrence and periodic series values aligned with the API contract."""
    assert {item.value for item in TaskRecurrence} == {"one_off", "regular", "periodic"}
    assert SeriesFrequency.PERIODIC.value == "periodic"


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
