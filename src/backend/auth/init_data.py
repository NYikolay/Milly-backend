from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Annotated, NoReturn
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from backend.core.errors import UnauthorizedError

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_INVALID_INIT_DATA = "Invalid init data"


class TelegramUser(BaseModel):
    """Validated Telegram user payload extracted from signed init data."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: Annotated[StrictInt, Field(gt=0, lt=2**63)]
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False
    is_bot: bool | None = None
    added_to_attachment_menu: bool | None = None
    allows_write_to_pm: bool | None = None
    photo_url: str | None = None


def _invalid() -> NoReturn:
    """Raise the opaque error used for every rejected init-data payload."""
    raise UnauthorizedError(_INVALID_INIT_DATA)


def validate_init_data(
    raw_init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int,
    max_future_skew_seconds: int = 60,
    now: datetime | None = None,
) -> TelegramUser:
    """
    Validate signed Telegram Mini App init data and return its user.

    Purpose:
        Authenticate Mini App launches by verifying that Telegram signed the
        raw init data, that it is recent, and that it describes a regular user.

    Args:
        raw_init_data: Raw ``Telegram.WebApp.initData`` query string.
        bot_token: Telegram bot token used to derive the HMAC secret.
        max_age_seconds: Maximum accepted age of ``auth_date`` in seconds.
        max_future_skew_seconds: Maximum accepted clock skew into the future.
        now: Current time used for freshness checks. Defaults to the current
            UTC time.

    Returns:
        TelegramUser: Validated user payload from the init data.

    Raises:
        UnauthorizedError: If the payload is empty, malformed, duplicated,
            incorrectly signed, stale, or contains an invalid or bot user.

    Notes:
        - Every rejection uses the same message so the failed check is not
          disclosed to the client.
        - The ``signature`` field remains part of the signed data-check-string.
        - Raw init data is never logged or included in error details.
    """
    if not raw_init_data:
        _invalid()

    try:
        pairs = parse_qsl(raw_init_data, keep_blank_values=True)
    except ValueError:
        _invalid()

    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            _invalid()
        fields[key] = value

    if not {"hash", "auth_date", "user"}.issubset(fields):
        _invalid()

    provided_hash = fields["hash"]
    if _HASH_PATTERN.fullmatch(provided_hash) is None:
        _invalid()

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"
    )
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, provided_hash):
        _invalid()

    try:
        auth_date = int(fields["auth_date"])
    except ValueError:
        _invalid()

    current = now if now is not None else datetime.now(UTC)
    age_seconds = int(current.timestamp()) - auth_date
    if age_seconds > max_age_seconds or age_seconds < -max_future_skew_seconds:
        _invalid()

    try:
        user_payload = json.loads(fields["user"])
    except json.JSONDecodeError, TypeError:
        _invalid()

    try:
        user = TelegramUser.model_validate(user_payload)
    except ValidationError:
        _invalid()

    if user.is_bot is True:
        _invalid()

    return user
