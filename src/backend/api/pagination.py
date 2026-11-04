import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Final

from backend.core.errors import BadRequestError

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100
MAX_CURSOR_LENGTH: Final = 512
MAX_DB_ID: Final = 2**63 - 1

_CURSOR_VERSION: Final = 1
_CURSOR_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]+")


def filter_fingerprint(values: Mapping[str, object]) -> str:
    """
    Build a stable fingerprint for the filters that produced a page.

    Purpose:
        Bind a cursor to the filter set it was issued for so a cursor cannot
        be replayed with different filters.

    Args:
        values: Filter values keyed by parameter name.

    Returns:
        str: Hex-encoded SHA-256 digest of the canonical JSON representation.

    Notes:
        - Dictionary key order does not affect the digest.
        - Unsupported values are serialized through ``str()``.
    """
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def encode_cursor(
    *,
    user_id: int,
    filter_fingerprint: str,
    order_key: Sequence[object],
) -> str:
    """
    Encode a cursor for the next page of a list response.

    Purpose:
        Produce an opaque, URL-safe token carrying the owner, filter binding,
        and ordering key of the last item on the current page.

    Args:
        user_id: Identifier of the authenticated user the cursor belongs to.
        filter_fingerprint: Fingerprint of the filters used for the page.
        order_key: Ordering values of the last item on the page.

    Returns:
        str: URL-safe Base64 token without padding.

    Notes:
        - No side effects; the token is not encrypted, only obfuscated.
    """
    payload = {
        "v": _CURSOR_VERSION,
        "u": user_id,
        "f": filter_fingerprint,
        "k": list(order_key),
    }
    encoded = json.dumps(payload, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, user_id: int, filter_fingerprint: str) -> list[object]:
    """
    Decode and validate an opaque pagination cursor.

    Purpose:
        Restore the ordering key for the next page while rejecting malformed
        tokens, unsupported versions, foreign users, and changed filters.

    Args:
        cursor: Token previously produced by :func:`encode_cursor`.
        user_id: Identifier of the authenticated user making the request.
        filter_fingerprint: Fingerprint of the filters used for this request.

    Returns:
        list[object]: Ordering values stored in the cursor.

    Raises:
        BadRequestError: If the cursor is empty, too long, not URL-safe
            Base64, not a JSON object, of an unsupported version, bound to
            another user or filter set, or missing a list ordering key.

    Notes:
        - No side effects; every decode failure maps to one generic error so
          malformed tokens do not leak implementation details.
    """
    try:
        if (
            not cursor
            or len(cursor) > MAX_CURSOR_LENGTH
            or _CURSOR_PATTERN.fullmatch(cursor) is None
        ):
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        if type(data.get("v")) is not int or data.get("v") != _CURSOR_VERSION:
            raise ValueError
        if type(data.get("u")) is not int or data.get("u") != user_id:
            raise ValueError
        if data.get("f") != filter_fingerprint:
            raise ValueError
        order_key = data.get("k")
        if not isinstance(order_key, list):
            raise ValueError
        return order_key
    except Exception:
        raise BadRequestError("Invalid pagination cursor") from None
