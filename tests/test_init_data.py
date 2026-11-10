from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from backend.application.auth.errors import AuthNotConfiguredError
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.domain.auth.models import TelegramUser
from backend.infrastructure.auth.init_data import (
    PydanticInitDataValidator,
    validate_init_data,
)
from tests.auth_helpers import BOT_TOKEN, NOW, _hash, make_init_data


def test_accepts_valid_init_data() -> None:
    """Return the Telegram user from valid signed init data."""
    result = validate_init_data(
        make_init_data(),
        bot_token=BOT_TOKEN,
        max_age_seconds=3600,
        now=NOW,
    )
    assert result.id == 123
    assert result.first_name == "Ivan"
    assert result.username == "ivan"


def test_rejects_tampered_field() -> None:
    """Reject init data whose signed fields were modified."""
    raw = make_init_data().replace("Ivan", "Mallory")
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_signature_is_covered_by_hmac() -> None:
    """Reject a signature field that was not part of the signed payload."""
    fields = {
        "auth_date": str(int(NOW.timestamp())),
        "user": json.dumps({"id": 123, "first_name": "Ivan"}),
        "signature": "signature-value",
    }
    raw = urlencode({**fields, "hash": _hash(fields, exclude=("signature",))})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_missing_hash() -> None:
    """Reject init data without a hash."""
    raw = make_init_data().split("&hash=")[0]
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_malformed_hash() -> None:
    """Reject init data whose hash is not 64 hexadecimal characters."""
    raw = make_init_data().rsplit("hash=", 1)[0] + "hash=not-hex"
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_duplicate_keys() -> None:
    """Reject init data containing the same field more than once."""
    raw = make_init_data() + f"&auth_date={int(NOW.timestamp())}"
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_stale_auth_date() -> None:
    """Reject init data older than the configured maximum age."""
    raw = make_init_data(auth_date=int(NOW.timestamp()) - 3601)
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_future_auth_date() -> None:
    """Reject init data dated beyond the allowed future skew."""
    raw = make_init_data(auth_date=int(NOW.timestamp()) + 61)
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_missing_user() -> None:
    """Reject init data without a user field."""
    fields = {"auth_date": str(int(NOW.timestamp()))}
    raw = urlencode({**fields, "hash": _hash(fields)})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_invalid_user_json() -> None:
    """Reject init data whose user field is not valid JSON."""
    fields = {"auth_date": str(int(NOW.timestamp())), "user": "{"}
    raw = urlencode({**fields, "hash": _hash(fields)})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_non_integer_user_id() -> None:
    """Reject a user ID that is not a strict integer."""
    raw = make_init_data(user={"id": "123", "first_name": "Ivan"})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


@pytest.mark.parametrize("user_id", [0, -1])
def test_rejects_non_positive_user_id(user_id: int) -> None:
    """Reject a user ID that is not greater than zero."""
    raw = make_init_data(user={"id": user_id, "first_name": "Ivan"})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_bot_user() -> None:
    """Reject init data belonging to a bot account."""
    raw = make_init_data(user={"id": 123, "first_name": "Bot", "is_bot": True})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_empty_init_data() -> None:
    """Reject an empty init-data string."""
    with pytest.raises(UnauthorizedError):
        validate_init_data("", bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_missing_auth_date() -> None:
    """Reject init data without an auth_date timestamp."""
    fields = {"user": json.dumps({"id": 123, "first_name": "Ivan"})}
    raw = urlencode({**fields, "hash": _hash(fields)})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_missing_first_name() -> None:
    """Reject a user payload without a first name."""
    raw = make_init_data(user={"id": 123})
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token=BOT_TOKEN, max_age_seconds=3600, now=NOW)


def test_rejects_wrong_bot_token() -> None:
    """Reject init data signed with a different bot token."""
    raw = make_init_data()
    with pytest.raises(UnauthorizedError):
        validate_init_data(raw, bot_token="other-token", max_age_seconds=3600, now=NOW)


def test_accepts_missing_signature() -> None:
    """Accept init data without the optional signature field."""
    result = validate_init_data(
        make_init_data(signature=None),
        bot_token=BOT_TOKEN,
        max_age_seconds=3600,
        now=NOW,
    )
    assert result.id == 123


def test_accepts_missing_query_id() -> None:
    """Accept init data without the optional query ID field."""
    result = validate_init_data(
        make_init_data(query_id=None),
        bot_token=BOT_TOKEN,
        max_age_seconds=3600,
        now=NOW,
    )
    assert result.id == 123


def test_module_validator_returns_domain_user() -> None:
    """Return a plain domain TelegramUser from valid signed init data."""
    result = validate_init_data(
        make_init_data(),
        bot_token=BOT_TOKEN,
        max_age_seconds=3600,
        now=NOW,
    )
    assert isinstance(result, TelegramUser)
    assert type(result) is TelegramUser


def test_init_data_validator_returns_domain_user() -> None:
    """The Pydantic adapter returns a domain TelegramUser."""
    settings = Settings(_env_file=None, telegram_bot_token=BOT_TOKEN)
    validator = PydanticInitDataValidator(settings)
    fresh_init_data = make_init_data(auth_date=int(datetime.now(UTC).timestamp()))

    user = validator.validate(fresh_init_data)

    assert isinstance(user, TelegramUser)
    assert type(user) is TelegramUser
    assert user.id == 123
    assert user.first_name == "Ivan"


def test_init_data_validator_requires_bot_token() -> None:
    """Reject validation when the adapter has no Telegram bot token."""
    validator = PydanticInitDataValidator(Settings(_env_file=None))

    with pytest.raises(AuthNotConfiguredError):
        validator.validate(make_init_data())
