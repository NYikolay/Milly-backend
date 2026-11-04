import base64
import json

import pytest

from backend.api.pagination import decode_cursor, encode_cursor, filter_fingerprint
from backend.api.task_schemas import ProjectListResponse, TaskListResponse
from backend.core.errors import BadRequestError


def test_cursor_roundtrip() -> None:
    """Round-trip an order key through the opaque cursor codec."""
    token = encode_cursor(
        user_id=7,
        filter_fingerprint="abc",
        order_key=["2026-11-04T09:00:00+00:00", 42],
    )
    assert "=" not in token
    assert decode_cursor(token, user_id=7, filter_fingerprint="abc") == [
        "2026-11-04T09:00:00+00:00",
        42,
    ]


def test_cursor_rejects_wrong_user() -> None:
    """Reject a cursor issued for another user."""
    token = encode_cursor(user_id=7, filter_fingerprint="abc", order_key=[1])
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=8, filter_fingerprint="abc")


def test_cursor_rejects_changed_filters() -> None:
    """Reject a cursor issued for a different filter set."""
    token = encode_cursor(user_id=7, filter_fingerprint="abc", order_key=[1])
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=7, filter_fingerprint="def")


@pytest.mark.parametrize("token", ["", "not-base64!", "e30", "eyJ2IjoyfQ"])
def test_cursor_rejects_invalid_payloads(token: str) -> None:
    """Reject malformed, empty, and unsupported-version cursors."""
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=7, filter_fingerprint="abc")


def test_filter_fingerprint_is_dict_key_order_insensitive() -> None:
    """Fingerprint equal mappings identically regardless of key order."""
    assert filter_fingerprint({"a": 1, "b": 2}) == filter_fingerprint({"b": 2, "a": 1})


def test_list_responses_expose_next_cursor() -> None:
    """Expose an optional next cursor on both paginated list responses."""
    assert TaskListResponse(items=[]).next_cursor is None
    assert ProjectListResponse(items=[]).next_cursor is None
    assert TaskListResponse(items=[], next_cursor="task-token").next_cursor == "task-token"
    assert ProjectListResponse(items=[], next_cursor="project-token").next_cursor == "project-token"


def _encode_payload(payload: object) -> str:
    """Encode a cursor payload without using the production encoder."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize("token", ["abc+def", "abc/def", "abc def", "abc$def"])
def test_cursor_rejects_non_url_safe_alphabet(token: str) -> None:
    """Reject junk and standard-Base64 characters outside the URL-safe alphabet."""
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=7, filter_fingerprint="abc")


@pytest.mark.parametrize(
    "payload",
    [
        {"v": True, "u": 7, "f": "abc", "k": [1]},
        {"v": 1.0, "u": 7, "f": "abc", "k": [1]},
        {"v": 1, "u": True, "f": "abc", "k": [1]},
        {"v": 1, "u": 7.0, "f": "abc", "k": [1]},
    ],
)
def test_cursor_rejects_non_integer_version_and_user(payload: dict[str, object]) -> None:
    """Reject boolean and floating-point cursor version or user identifiers."""
    with pytest.raises(BadRequestError):
        decode_cursor(
            _encode_payload(payload),
            user_id=7,
            filter_fingerprint="abc",
        )


@pytest.mark.parametrize("order_key", ["not-a-list", {"key": 1}, 42, None])
def test_cursor_rejects_non_list_order_key(order_key: object) -> None:
    """Reject cursor payloads whose ordering key is not a list."""
    payload = {"v": 1, "u": 7, "f": "abc", "k": order_key}

    with pytest.raises(BadRequestError):
        decode_cursor(
            _encode_payload(payload),
            user_id=7,
            filter_fingerprint="abc",
        )


def test_cursor_rejects_token_longer_than_limit() -> None:
    """Reject a token longer than the shared cursor length limit."""
    with pytest.raises(BadRequestError):
        decode_cursor("x" * 513, user_id=7, filter_fingerprint="abc")
