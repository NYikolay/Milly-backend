"""Framework-independent authentication DTOs and settings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.domain.entities import TelegramProfile, User


@dataclass(frozen=True)
class AccessTokenClaims:
    """Validated identity and session claims extracted from an access token."""

    user_id: int
    session_id: int
    telegram_id: int
    jti: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class TokenPair:
    """Issued access and refresh token values with their expiry times."""

    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


@dataclass(frozen=True)
class LoginResult:
    """Outcome of a successful Telegram login."""

    user: User
    profile: TelegramProfile
    tokens: TokenPair
    is_new: bool


@dataclass(frozen=True)
class TelegramUser:
    """Validated Telegram user payload extracted from signed init data."""

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False
    is_bot: bool | None = None
    added_to_attachment_menu: bool | None = None
    allows_write_to_pm: bool | None = None
    photo_url: str | None = None


@dataclass(frozen=True)
class TelegramIdentity:
    """Application user paired with the Telegram profile that identifies them."""

    user: User
    profile: TelegramProfile


@dataclass(frozen=True)
class AuthSettings:
    """Plain authentication configuration passed into the application layer."""

    telegram_bot_token: str | None = None
    auth_jwt_secret: str | None = None
    auth_jwt_issuer: str = "milly"
    auth_jwt_leeway_seconds: int = 30
    telegram_init_data_max_age_seconds: int = 3600
    telegram_init_data_max_future_skew_seconds: int = 60
    auth_access_ttl_seconds: int = 900
    auth_refresh_ttl_seconds: int = 2592000
    auth_refresh_reuse_grace_seconds: int = 30
