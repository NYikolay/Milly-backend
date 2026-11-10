"""Pydantic-backed Telegram init-data validation adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Annotated, NoReturn
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from backend.application.auth.errors import AuthNotConfiguredError
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.domain.auth.models import TelegramUser

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_INVALID_INIT_DATA = "Invalid init data"


class _InitDataUserPayload(BaseModel):
    """Pydantic shape used to validate the signed Telegram user object."""

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


def _to_domain_user(payload: _InitDataUserPayload) -> TelegramUser:
    """
    Convert a validated Pydantic payload into the domain user DTO.

    Purpose:
        Keep Pydantic models confined to the adapter and return the plain
        dataclass consumed by the application layer.

    Args:
        payload: Validated Telegram user object.

    Returns:
        TelegramUser: Framework-independent Telegram identity payload.
    """
    return TelegramUser(
        id=payload.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        username=payload.username,
        language_code=payload.language_code,
        is_premium=payload.is_premium,
        is_bot=payload.is_bot,
        added_to_attachment_menu=payload.added_to_attachment_menu,
        allows_write_to_pm=payload.allows_write_to_pm,
        photo_url=payload.photo_url,
    )


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
        TelegramUser: Validated domain user payload from the init data.

    Raises:
        UnauthorizedError: If the payload is empty, malformed, duplicated,
            incorrectly signed, stale, or contains an invalid or bot user.

    Notes:
        - Every rejection uses the same message so the failed check is not
          disclosed to the client.
        - A naive ``now`` is interpreted as UTC.
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
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    age_seconds = int(current.timestamp()) - auth_date
    if age_seconds > max_age_seconds or age_seconds < -max_future_skew_seconds:
        _invalid()

    try:
        user_payload = json.loads(fields["user"])
    except json.JSONDecodeError:
        raise UnauthorizedError(_INVALID_INIT_DATA) from None

    try:
        payload = _InitDataUserPayload.model_validate(user_payload)
    except ValidationError:
        raise UnauthorizedError(_INVALID_INIT_DATA) from None

    if payload.is_bot is True:
        _invalid()

    return _to_domain_user(payload)


class PydanticInitDataValidator:
    """Init-data validator adapter backed by Pydantic and application settings."""

    def __init__(self, settings: Settings) -> None:
        """
        Initialize the validator with the application settings.

        Purpose:
            Read the Telegram bot token and freshness limits needed to verify
            signed Mini App init data.

        Args:
            settings: Application settings containing Telegram configuration.
        """
        self._settings = settings

    def validate(self, raw_init_data: str) -> TelegramUser:
        """
        Validate signed Telegram init data and return its domain user.

        Purpose:
            Implement the application ``InitDataValidator`` port while keeping
            HMAC, Pydantic parsing, freshness, and bot rejection in the
            infrastructure layer.

        Args:
            raw_init_data: Raw ``Telegram.WebApp.initData`` query string.

        Returns:
            TelegramUser: Validated domain user payload from the init data.

        Raises:
            AuthNotConfiguredError: If the Telegram bot token is not configured.
            UnauthorizedError: If the payload is empty, malformed, duplicated,
                incorrectly signed, stale, or contains a bot user.

        Notes:
            - Freshness is checked against the current UTC time.
            - Every rejection uses the same opaque message so failed checks are
              not disclosed to the client.
            - Raw init data is never logged or included in error details.
        """
        bot_token = self._settings.telegram_bot_token
        if bot_token is None:
            raise AuthNotConfiguredError()
        return validate_init_data(
            raw_init_data,
            bot_token=bot_token.get_secret_value(),
            max_age_seconds=self._settings.telegram_init_data_max_age_seconds,
            max_future_skew_seconds=(self._settings.telegram_init_data_max_future_skew_seconds),
        )
