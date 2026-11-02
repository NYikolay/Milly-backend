from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from backend.auth.errors import AuthNotConfiguredError
from backend.auth.tokens import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SECRET = "a" * 32


def _settings(**overrides: Any) -> Settings:
    """Build auth settings with a deterministic JWT secret."""
    return Settings(
        _env_file=None,
        auth_jwt_secret=SECRET,
        telegram_bot_token="token",
        **overrides,
    )


def _token_with(payload_overrides: dict[str, object]) -> str:
    """Encode an HS256 token with the standard access-token payload."""
    payload: dict[str, object] = {
        "sub": "1",
        "sid": "2",
        "telegram_id": 3,
        "jti": "j",
        "type": "access",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(seconds=900)).timestamp()),
        "iss": "milly",
    }
    payload.update(payload_overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_access_token_round_trip() -> None:
    """Encode and decode a token while preserving its identity claims."""
    token, expires_at = create_access_token(1, 2, 3, settings=_settings(), now=NOW)
    claims = decode_access_token(token, settings=_settings(), now=NOW)
    assert claims.user_id == 1
    assert claims.session_id == 2
    assert claims.telegram_id == 3
    assert claims.jti
    assert expires_at == NOW + timedelta(seconds=900)
    assert claims.expires_at == expires_at


def test_decode_rejects_wrong_secret() -> None:
    """Reject a token signed with a different JWT secret."""
    token, _ = create_access_token(1, 2, 3, settings=_settings(), now=NOW)
    other = Settings(
        _env_file=None,
        auth_jwt_secret="b" * 32,
        telegram_bot_token="token",
    )
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, settings=other, now=NOW)


def test_decode_rejects_expired_token() -> None:
    """Reject a token expired beyond the configured leeway."""
    token, _ = create_access_token(1, 2, 3, settings=_settings(), now=NOW)
    with pytest.raises(UnauthorizedError):
        decode_access_token(
            token,
            settings=_settings(),
            now=NOW + timedelta(seconds=931),
        )


def test_decode_accepts_token_expired_within_leeway() -> None:
    """Accept a token expired by no more than the configured leeway."""
    token, _ = create_access_token(1, 2, 3, settings=_settings(), now=NOW)
    claims = decode_access_token(
        token,
        settings=_settings(),
        now=NOW + timedelta(seconds=930),
    )
    assert claims.user_id == 1


def test_decode_rejects_future_issued_at_beyond_leeway() -> None:
    """Reject a token issued too far in the future."""
    token = _token_with({"iat": int((NOW + timedelta(seconds=31)).timestamp())})
    with pytest.raises(UnauthorizedError):
        decode_access_token(token, settings=_settings(), now=NOW)


def test_decode_rejects_wrong_type() -> None:
    """Reject a token whose type claim is not access."""
    with pytest.raises(UnauthorizedError):
        decode_access_token(_token_with({"type": "refresh"}), settings=_settings(), now=NOW)


def test_decode_rejects_wrong_issuer() -> None:
    """Reject a token whose issuer does not match the settings."""
    with pytest.raises(UnauthorizedError):
        decode_access_token(_token_with({"iss": "other"}), settings=_settings(), now=NOW)


def test_decode_rejects_malformed_sub() -> None:
    """Reject a token whose subject is not an integer."""
    with pytest.raises(UnauthorizedError):
        decode_access_token(_token_with({"sub": "not-an-int"}), settings=_settings(), now=NOW)


def test_create_requires_jwt_secret() -> None:
    """Refuse to issue tokens when no JWT secret is configured."""
    with pytest.raises(AuthNotConfiguredError):
        create_access_token(
            1,
            2,
            3,
            settings=Settings(_env_file=None, telegram_bot_token="token"),
            now=NOW,
        )


def test_decode_requires_jwt_secret() -> None:
    """Refuse to verify tokens when no JWT secret is configured."""
    token, _ = create_access_token(1, 2, 3, settings=_settings(), now=NOW)
    with pytest.raises(AuthNotConfiguredError):
        decode_access_token(
            token,
            settings=Settings(_env_file=None, telegram_bot_token="token"),
            now=NOW,
        )


def test_refresh_tokens_are_random_and_hashed() -> None:
    """Generate unique refresh tokens and hash them deterministically."""
    first = generate_refresh_token()
    second = generate_refresh_token()
    assert first != second
    assert len(first) >= 43
    assert len(hash_refresh_token(first)) == 64
    assert hash_refresh_token(first) == hash_refresh_token(first)
