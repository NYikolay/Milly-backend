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
