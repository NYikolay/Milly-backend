"""Task-list cursor parsing, encoding, and filter fingerprint policy."""

from __future__ import annotations

from datetime import datetime

from backend.api.pagination import (
    MAX_DB_ID,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)
from backend.api.task_schemas import TaskFiltersQuery
from backend.core.errors import BadRequestError
from backend.domain.task_management.models import TaskListCursor


def task_filter_fingerprint(filters: TaskFiltersQuery) -> str:
    """
    Build the canonical fingerprint of task-list filters.

    Purpose:
        Bind a pagination cursor to the exact filter set that produced it.

    Args:
        filters: Validated task-list query parameters.

    Returns:
        str: Stable digest of the canonical filter mapping.

    Notes:
        - ``project_ids`` and ``priorities`` are sorted; ``q`` is unchanged so
          whitespace and casing keep affecting the fingerprint.
    """
    return filter_fingerprint(
        {
            "date": filters.date.isoformat() if filters.date is not None else None,
            "include_archived_projects": filters.include_archived_projects,
            "priorities": sorted(priority.value for priority in filters.priorities),
            "project_ids": sorted(filters.project_ids),
            "q": filters.q,
            "recurrence": filters.recurrence,
            "status": filters.status,
        }
    )


def parse_task_cursor(
    cursor: str,
    *,
    user_id: int,
    filter_fingerprint: str,
) -> TaskListCursor:
    """
    Decode and validate the ordering key of a task-list cursor.

    Purpose:
        Restore the typed ordering key for the next page while rejecting
        malformed tokens, foreign owners, and changed filter sets.

    Args:
        cursor: Opaque token previously returned by
            :func:`encode_task_cursor`.
        user_id: Identifier of the authenticated user making the request.
        filter_fingerprint: Fingerprint of the task-list filters used for this
            request.

    Returns:
        TaskListCursor: The aware due time and task identifier of the last
            returned task.

    Raises:
        BadRequestError: If the token is invalid, belongs to another user or
            filter set, or does not contain exactly an aware ISO-8601 due time
            and an integer task identifier within the database range.
    """
    order_key = decode_cursor(
        cursor,
        user_id=user_id,
        filter_fingerprint=filter_fingerprint,
    )
    try:
        if len(order_key) != 2:
            raise ValueError
        due_value, task_id = order_key
        if (
            not isinstance(due_value, str)
            or type(task_id) is not int
            or not 1 <= task_id <= MAX_DB_ID
        ):
            raise ValueError
        due_at = datetime.fromisoformat(due_value)
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):  # fmt: skip
        raise BadRequestError("Invalid pagination cursor") from None
    return TaskListCursor(due_at=due_at, task_id=task_id)


def encode_task_cursor(
    cursor: TaskListCursor,
    *,
    user_id: int,
    filter_fingerprint: str,
) -> str:
    """
    Encode a task cursor for the next page.

    Purpose:
        Produce an opaque token carrying the owner, filter binding, and
        ordering key of the last task on the current page.

    Args:
        cursor: Ordering key of the last task on the current page.
        user_id: Identifier of the authenticated user the cursor belongs to.
        filter_fingerprint: Fingerprint of the task-list filters used for the
            page.

    Returns:
        str: URL-safe Base64 token without padding.

    Notes:
        - No side effects; the token is not encrypted, only obfuscated.
    """
    return encode_cursor(
        user_id=user_id,
        filter_fingerprint=filter_fingerprint,
        order_key=[cursor.due_at.isoformat(), cursor.task_id],
    )
