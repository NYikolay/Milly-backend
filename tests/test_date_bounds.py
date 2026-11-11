from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.domain.task_management.policies import (
    MAX_SUPPORTED_DATE,
    MAX_SUPPORTED_DATETIME,
    MIN_SUPPORTED_DATE,
    MIN_SUPPORTED_DATETIME,
    require_supported_date,
    require_supported_datetime,
)


def test_require_supported_date_accepts_bounds() -> None:
    """Accept both inclusive calendar-date boundaries."""
    assert require_supported_date(date(2025, 1, 1)) == date(2025, 1, 1)
    assert require_supported_date(date(2100, 12, 31)) == date(2100, 12, 31)


def test_require_supported_date_rejects_outside() -> None:
    """Reject calendar dates one day outside either boundary."""
    with pytest.raises(ValueError, match="supported range"):
        require_supported_date(date(2024, 12, 31))
    with pytest.raises(ValueError, match="supported range"):
        require_supported_date(date(2101, 1, 1))


def test_date_bounds_match_the_agreed_contract() -> None:
    """Expose the agreed inclusive calendar-date limits."""
    assert date(2025, 1, 1) == MIN_SUPPORTED_DATE
    assert date(2100, 12, 31) == MAX_SUPPORTED_DATE


def test_require_supported_datetime_accepts_bounds() -> None:
    """Accept both inclusive UTC datetime boundaries."""
    assert require_supported_datetime(MIN_SUPPORTED_DATETIME) == MIN_SUPPORTED_DATETIME
    assert require_supported_datetime(MAX_SUPPORTED_DATETIME) == MAX_SUPPORTED_DATETIME


def test_require_supported_datetime_rejects_outside() -> None:
    """Reject instants one second outside either boundary."""
    with pytest.raises(ValueError, match="supported range"):
        require_supported_datetime(MIN_SUPPORTED_DATETIME - timedelta(seconds=1))
    with pytest.raises(ValueError, match="supported range"):
        require_supported_datetime(MAX_SUPPORTED_DATETIME + timedelta(seconds=1))


def test_datetime_bounds_match_the_agreed_contract() -> None:
    """Expose the agreed inclusive UTC datetime limits."""
    assert datetime(2025, 1, 1, tzinfo=UTC) == MIN_SUPPORTED_DATETIME
    assert datetime(2100, 12, 31, 23, 59, 59, 999999, tzinfo=UTC) == MAX_SUPPORTED_DATETIME


def test_require_supported_datetime_rejects_naive() -> None:
    """Reject naive values before comparing them with UTC bounds."""
    with pytest.raises(ValueError, match="timezone-aware"):
        require_supported_datetime(datetime(2026, 11, 4, 12, 0))


def test_require_supported_datetime_compares_offsets_in_utc() -> None:
    """Compare offset instants against the UTC bounds, not their wall clock."""
    moscow = ZoneInfo("Europe/Moscow")
    assert require_supported_datetime(datetime(2025, 1, 1, 3, 0, tzinfo=moscow)) == datetime(
        2025, 1, 1, 3, 0, tzinfo=moscow
    )
    with pytest.raises(ValueError, match="supported range"):
        require_supported_datetime(datetime(2025, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=1))))
