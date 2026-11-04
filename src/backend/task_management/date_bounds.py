"""Shared inclusive bounds for user-supplied calendar dates and instants."""

from __future__ import annotations

from datetime import UTC, date, datetime

MIN_SUPPORTED_DATE: date = date(2025, 1, 1)
MAX_SUPPORTED_DATE: date = date(2100, 12, 31)
MIN_SUPPORTED_DATETIME: datetime = datetime(2025, 1, 1, tzinfo=UTC)
MAX_SUPPORTED_DATETIME: datetime = datetime(2100, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


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
