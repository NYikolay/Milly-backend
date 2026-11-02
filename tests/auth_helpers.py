from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import urlencode

BOT_TOKEN = "test-bot-token"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _hash(
    fields: dict[str, str],
    *,
    exclude: tuple[str, ...] = (),
    bot_token: str = BOT_TOKEN,
) -> str:
    """
    Compute the Telegram init-data HMAC for the given fields.

    Purpose:
        Build a signed payload in tests that matches Telegram's algorithm.

    Args:
        fields: Raw init-data fields excluding the hash.
        exclude: Field names to omit from the data-check-string.
        bot_token: Bot token used as the HMAC key material.

    Returns:
        str: Lowercase hexadecimal HMAC-SHA256 signature.
    """
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key not in exclude
    )
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()


def make_init_data(
    *,
    user: dict[str, object] | None = None,
    auth_date: int | None = None,
    query_id: str | None = "query-1",
    signature: str | None = "signature-value",
    bot_token: str = BOT_TOKEN,
) -> str:
    """
    Build a signed Telegram init-data query string.

    Purpose:
        Produce representative init data for validation tests with control
        over the user payload, timestamp, and optional fields.

    Args:
        user: Telegram user object. Defaults to a regular user.
        auth_date: Unix timestamp in seconds. Defaults to the test clock.
        query_id: Optional query ID; omitted when ``None``.
        signature: Optional signature field; omitted when ``None``.
        bot_token: Bot token used to sign the payload.

    Returns:
        str: URL-encoded init data including the computed hash.
    """
    fields = {
        "auth_date": str(int(NOW.timestamp()) if auth_date is None else auth_date),
        "user": json.dumps(user or {"id": 123, "first_name": "Ivan", "username": "ivan"}),
    }
    if query_id is not None:
        fields["query_id"] = query_id
    if signature is not None:
        fields["signature"] = signature
    fields["hash"] = _hash(fields, bot_token=bot_token)
    return urlencode(fields)
