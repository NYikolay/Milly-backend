"""JWT access-token adapter and compatibility token helpers."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from backend.application.auth.errors import AuthNotConfiguredError
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.domain.auth.models import AccessTokenClaims as AccessTokenClaims
from backend.domain.auth.tokens import generate_refresh_token as generate_refresh_token
from backend.domain.auth.tokens import hash_refresh_token as hash_refresh_token

_ALGORITHM = "HS256"
_INVALID_TOKEN = "Invalid access token"
_REQUIRED_CLAIMS = ("exp", "iat", "iss", "sub", "sid", "telegram_id", "jti", "type")


def _jwt_secret(settings: Settings) -> str:
    """
    Return the configured JWT signing secret.

    Purpose:
        Centralize the check that authentication is configured before any
        token is signed or verified.

    Args:
        settings: Application settings containing the optional JWT secret.

    Returns:
        str: The raw JWT secret.

    Raises:
        AuthNotConfiguredError: If ``auth_jwt_secret`` is not configured.
    """
    if settings.auth_jwt_secret is None:
        raise AuthNotConfiguredError()
    return settings.auth_jwt_secret.get_secret_value()


def _as_utc(value: datetime) -> datetime:
    """
    Normalize a datetime to timezone-aware UTC.

    Purpose:
        Support callers that pass either naive or offset-aware datetimes while
        keeping all token timestamps comparable.

    Args:
        value: Datetime to normalize.

    Returns:
        datetime: The same instant in UTC, with naive values treated as UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _claim_int(payload: Mapping[str, Any], name: str) -> int:
    """
    Read an integer claim from a decoded token payload.

    Purpose:
        Convert JWT identity claims that may be encoded as strings or numbers
        and reject values that cannot represent an integer.

    Args:
        payload: Decoded JWT payload.
        name: Claim name to read.

    Returns:
        int: The converted claim value.

    Raises:
        UnauthorizedError: If the claim is missing, boolean, or not convertible
            to an integer.
    """
    value = payload.get(name)
    if isinstance(value, bool):
        raise UnauthorizedError(_INVALID_TOKEN)
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise UnauthorizedError(_INVALID_TOKEN)
    try:
        return int(value)
    except ValueError:
        raise UnauthorizedError(_INVALID_TOKEN) from None


def _claim_timestamp(payload: Mapping[str, Any], name: str) -> datetime:
    """
    Read a numeric JWT timestamp claim as a UTC datetime.

    Purpose:
        Convert standard ``iat`` and ``exp`` NumericDate claims without relying
        on PyJWT's built-in time validation.

    Args:
        payload: Decoded JWT payload.
        name: Claim name to read.

    Returns:
        datetime: The claim value as a timezone-aware UTC datetime.

    Raises:
        UnauthorizedError: If the claim is missing, not numeric, or outside the
            representable datetime range.
    """
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UnauthorizedError(_INVALID_TOKEN)
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except OverflowError:
        raise UnauthorizedError(_INVALID_TOKEN) from None
    except OSError:
        raise UnauthorizedError(_INVALID_TOKEN) from None
    except ValueError:
        raise UnauthorizedError(_INVALID_TOKEN) from None


def create_access_token(
    user_id: int,
    session_id: int,
    telegram_id: int,
    *,
    settings: Settings,
    now: datetime,
) -> tuple[str, datetime]:
    """
    Issue a signed access token for a user session.

    Purpose:
        Create the short-lived bearer token returned after Telegram login or a
        successful refresh, carrying only immutable identity claims.

    Args:
        user_id: Internal user identifier.
        session_id: Authentication session identifier.
        telegram_id: Telegram user identifier.
        settings: Application settings with the JWT secret, issuer, and access
            token TTL.
        now: Issue time for the token.

    Returns:
        tuple[str, datetime]: The encoded HS256 token and its UTC expiry time.

    Raises:
        AuthNotConfiguredError: If no JWT secret is configured.

    Notes:
        - The payload deliberately excludes mutable or personally identifiable
          profile fields such as the username.
        - Timestamps are truncated to whole seconds to match JWT NumericDate
          precision.
    """
    secret = _jwt_secret(settings)
    issued_at = _as_utc(now).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=settings.auth_access_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "telegram_id": telegram_id,
        "jti": secrets.token_urlsafe(16),
        "type": "access",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.auth_jwt_issuer,
    }
    token = jwt.encode(payload, secret, algorithm=_ALGORITHM)
    return token, expires_at


def decode_access_token(
    token: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> AccessTokenClaims:
    """
    Verify and decode an access token into its claims.

    Purpose:
        Validate the signature, issuer, required claims, token type, and
        configured clock leeway before returning identity claims to request
        handlers.

    Args:
        token: Encoded access token.
        settings: Application settings with the JWT secret, issuer, and leeway.
        now: Current time for expiry and issue-time checks. Defaults to the
            current UTC time; naive values are treated as UTC.

    Returns:
        AccessTokenClaims: Validated domain claims from the token.

    Raises:
        AuthNotConfiguredError: If no JWT secret is configured.
        UnauthorizedError: If the token is malformed, incorrectly signed,
            expired beyond the leeway, issued too far in the future, has the
            wrong issuer or type, or contains invalid claims.
    """
    secret = _jwt_secret(settings)
    current = _as_utc(now) if now is not None else datetime.now(UTC)
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            issuer=settings.auth_jwt_issuer,
            options={
                "require": list(_REQUIRED_CLAIMS),
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.InvalidTokenError:
        raise UnauthorizedError(_INVALID_TOKEN) from None

    if payload.get("type") != "access":
        raise UnauthorizedError(_INVALID_TOKEN)
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise UnauthorizedError(_INVALID_TOKEN)
    issuer = payload.get("iss")
    if not isinstance(issuer, str) or not issuer:
        raise UnauthorizedError(_INVALID_TOKEN)

    issued_at = _claim_timestamp(payload, "iat")
    expires_at = _claim_timestamp(payload, "exp")
    leeway = timedelta(seconds=settings.auth_jwt_leeway_seconds)
    if expires_at < current - leeway:
        raise UnauthorizedError(_INVALID_TOKEN)
    if issued_at > current + leeway:
        raise UnauthorizedError(_INVALID_TOKEN)

    return AccessTokenClaims(
        user_id=_claim_int(payload, "sub"),
        session_id=_claim_int(payload, "sid"),
        telegram_id=_claim_int(payload, "telegram_id"),
        jti=jti,
        issued_at=issued_at,
        expires_at=expires_at,
    )


class JwtTokenIssuer:
    """Access-token issuer adapter backed by PyJWT and application settings."""

    def __init__(self, settings: Settings) -> None:
        """
        Initialize the issuer with the application settings.

        Purpose:
            Read the JWT secret, issuer, and access-token TTL needed to sign
            bearer tokens.

        Args:
            settings: Application settings containing JWT configuration.
        """
        self._settings = settings

    def issue_access_token(
        self,
        user_id: int,
        session_id: int,
        telegram_id: int,
        *,
        now: datetime,
    ) -> tuple[str, datetime]:
        """
        Issue a signed access token through the application port.

        Purpose:
            Keep PyJWT and settings handling in the infrastructure layer while
            preserving the exact claims, expiry, and signing behavior of the
            module-level helper.

        Args:
            user_id: Internal user identifier.
            session_id: Authentication session identifier.
            telegram_id: Telegram user identifier.
            now: Issue time for the token.

        Returns:
            tuple[str, datetime]: The encoded HS256 token and its UTC expiry
                time.

        Raises:
            AuthNotConfiguredError: If no JWT secret is configured.

        Notes:
            - Delegates to ``create_access_token`` so the module-level
              compatibility function and the adapter cannot drift.
        """
        return create_access_token(
            user_id,
            session_id,
            telegram_id,
            settings=self._settings,
            now=now,
        )
