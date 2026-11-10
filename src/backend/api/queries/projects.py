"""Project-list cursor parsing, encoding, and filter fingerprint policy."""

from __future__ import annotations

from backend.api.pagination import (
    MAX_DB_ID,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)
from backend.core.errors import BadRequestError
from backend.domain.task_management.models import ProjectListCursor


def project_filter_fingerprint(include_archived: bool) -> str:
    """
    Build the canonical fingerprint of project-list filters.

    Purpose:
        Bind a pagination cursor to the archive selection that produced it.

    Args:
        include_archived: Whether archived projects were included in the page.

    Returns:
        str: Stable digest of the canonical filter mapping.
    """
    return filter_fingerprint({"include_archived": include_archived})


def _is_safe_project_name_key(name_key: str) -> bool:
    """
    Check whether a project cursor name key is safe for a database comparison.

    Purpose:
        Reject NUL and surrogate code points that PostgreSQL text values
        cannot contain before the key is bound into a query.

    Args:
        name_key: Case-insensitive project name key decoded from a cursor.

    Returns:
        bool: ``True`` when the key contains no forbidden code points.
    """
    if "\x00" in name_key:
        return False
    return not any(0xD800 <= ord(character) <= 0xDFFF for character in name_key)


def parse_project_cursor(
    cursor: str,
    *,
    user_id: int,
    filter_fingerprint: str,
) -> ProjectListCursor:
    """
    Decode and validate the ordering key of a project-list cursor.

    Purpose:
        Restore the typed ordering key for the next page while rejecting
        malformed tokens, foreign owners, and changed filter sets.

    Args:
        cursor: Opaque token previously returned by
            :func:`encode_project_cursor`.
        user_id: Identifier of the authenticated user making the request.
        filter_fingerprint: Fingerprint of the project-list filters used for
            this request.

    Returns:
        ProjectListCursor: Archive state, case-insensitive name key, and
            identifier of the last project on the previous page.

    Raises:
        BadRequestError: If the token is invalid, belongs to another user or
            filter set, or does not contain an integer archive flag of ``0`` or
            ``1``, a database-safe string name key, and an integer project
            identifier within the database range.
    """
    order_key = decode_cursor(
        cursor,
        user_id=user_id,
        filter_fingerprint=filter_fingerprint,
    )
    try:
        if len(order_key) != 3:
            raise ValueError
        archived_value, name_key, project_id = order_key
        if type(archived_value) is not int or archived_value not in (0, 1):
            raise ValueError
        if not isinstance(name_key, str) or not _is_safe_project_name_key(name_key):
            raise ValueError
        if type(project_id) is not int or not 1 <= project_id <= MAX_DB_ID:
            raise ValueError
    except (TypeError, ValueError):  # fmt: skip
        raise BadRequestError("Invalid pagination cursor") from None
    return ProjectListCursor(
        is_archived=archived_value == 1,
        name_key=name_key,
        project_id=project_id,
    )


def encode_project_cursor(
    cursor: ProjectListCursor,
    *,
    user_id: int,
    filter_fingerprint: str,
) -> str:
    """
    Encode a project cursor for the next page.

    Purpose:
        Produce an opaque token carrying the owner, filter binding, and
        ordering key of the last project on the current page.

    Args:
        cursor: Ordering key of the last project on the current page.
        user_id: Identifier of the authenticated user the cursor belongs to.
        filter_fingerprint: Fingerprint of the project-list filters used for
            the page.

    Returns:
        str: URL-safe Base64 token without padding.

    Notes:
        - No side effects; the token is not encrypted, only obfuscated.
    """
    return encode_cursor(
        user_id=user_id,
        filter_fingerprint=filter_fingerprint,
        order_key=[
            1 if cursor.is_archived else 0,
            cursor.name_key,
            cursor.project_id,
        ],
    )
