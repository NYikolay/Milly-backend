from __future__ import annotations

from datetime import datetime

from backend.core.errors import BadRequestError
from backend.domain.enums import TaskRecurrence
from backend.domain.task_management.policies import require_supported_datetime

_TITLE_REQUIRED = "Task title is required"
_INVALID_DUE_AT = "Task due time must include a timezone"
_INVALID_RECURRENCE_INTERVAL = "Task recurrence and interval are incompatible"

__all__ = [
    "normalize_task_title",
    "validate_recurrence_interval",
    "validate_task_due_at",
]


def normalize_task_title(title: str) -> str:
    """
    Trim a task title and reject an empty result.

    Purpose:
        Apply the shared task title rule before writes.

    Args:
        title: Candidate title.

    Returns:
        str: The trimmed title.

    Raises:
        BadRequestError: If the trimmed title is empty.

    Notes:
        - Pure function with no side effects.
    """
    normalized = title.strip()
    if not normalized:
        raise BadRequestError(_TITLE_REQUIRED)
    return normalized


def validate_task_due_at(due_at: datetime) -> None:
    """
    Require a timezone-aware task due instant inside the supported range.

    Purpose:
        Keep stored due times as unambiguous absolute instants that schedule
        arithmetic can process safely.

    Args:
        due_at: Candidate due instant.

    Returns:
        None: The due instant is aware and supported.

    Raises:
        BadRequestError: If the due instant is naive or outside the inclusive
            UTC bounds.

    Notes:
        - Pure function with no side effects.
    """
    try:
        require_supported_datetime(due_at)
    except ValueError as error:
        raise BadRequestError(_INVALID_DUE_AT) from error


def validate_recurrence_interval(
    recurrence: TaskRecurrence | None,
    interval_days: int | None,
) -> None:
    """
    Validate a task recurrence mode and interval pair before any write.

    Purpose:
        Reject impossible combinations early so a toggle never leaves
        partially applied fields in the current unit of work.

    Args:
        recurrence: Requested recurrence mode, or ``None`` when unchanged.
        interval_days: Requested days between periodic occurrences, or ``None``
            when omitted.

    Returns:
        None: The pair is valid.

    Raises:
        BadRequestError: If ``one_off`` carries an interval, ``regular`` carries
            an interval other than ``1``, or periodic carries an interval below
            ``2``.

    Notes:
        - Pure function with no side effects.
    """
    if recurrence is TaskRecurrence.ONE_OFF and interval_days is not None:
        raise BadRequestError(_INVALID_RECURRENCE_INTERVAL)
    if recurrence is TaskRecurrence.REGULAR and interval_days not in (None, 1):
        raise BadRequestError(_INVALID_RECURRENCE_INTERVAL)
    if recurrence is TaskRecurrence.PERIODIC and interval_days is not None and interval_days < 2:
        raise BadRequestError(_INVALID_RECURRENCE_INTERVAL)
