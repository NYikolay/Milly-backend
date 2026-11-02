from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from backend.auth.errors import AuthNotConfiguredError
from backend.auth.init_data import validate_init_data
from backend.auth.repository import AuthRepository
from backend.auth.tokens import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.core.config import Settings
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


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


class AuthService:
    """Coordinates Telegram login, session issuance, and user lookup."""

    def __init__(
        self,
        repository: AuthRepository,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with its persistence and configuration ports.

        Purpose:
            Keep authentication logic independent of request handling and make
            all timestamps deterministic through an injectable clock.

        Args:
            repository: Persistence operations for profiles, users, and
                sessions.
            settings: Application settings with Telegram and JWT configuration.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never mutates ``settings``.
            - Normal login does not commit; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository
        self._settings = settings
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def login(self, raw_init_data: str) -> LoginResult:
        """
        Authenticate a Telegram Mini App launch and issue tokens.

        Purpose:
            Validate signed init data, lock the Telegram identity, create or
            refresh the local profile, and start a new refresh-session family.

        Args:
            raw_init_data: Raw ``Telegram.WebApp.initData`` query string.

        Returns:
            LoginResult: The user, profile, issued tokens, and whether the user
                was created by this login.

        Raises:
            AuthNotConfiguredError: If the Telegram bot token or JWT secret is
                not configured.
            UnauthorizedError: If the init data is invalid, stale, or belongs
                to a bot.

        Notes:
            - The advisory lock serializes concurrent logins for one Telegram
              identity.
            - Only the refresh-token hash is persisted; the raw token is
              returned to the caller only.
            - Does not commit; the request unit of work owns the transaction
              boundary.
            - Raw init data and tokens are never logged.
        """
        if self._settings.telegram_bot_token is None or self._settings.auth_jwt_secret is None:
            raise AuthNotConfiguredError()

        now = self._clock()
        data = validate_init_data(
            raw_init_data,
            bot_token=self._settings.telegram_bot_token.get_secret_value(),
            max_age_seconds=self._settings.telegram_init_data_max_age_seconds,
            max_future_skew_seconds=self._settings.telegram_init_data_max_future_skew_seconds,
            now=now,
        )

        await self._repository.acquire_telegram_lock(data.id)
        profile = await self._repository.get_profile_by_telegram_id(data.id)
        is_new = profile is None
        if profile is None:
            profile = await self._repository.create_user_with_profile(data)
        else:
            await self._repository.update_profile(profile, data)

        refresh_token = generate_refresh_token()
        refresh_expires_at = now + timedelta(seconds=self._settings.auth_refresh_ttl_seconds)
        session = await self._repository.create_session(
            user_id=profile.user_id,
            family_id=uuid4(),
            refresh_token_hash=hash_refresh_token(refresh_token),
            expires_at=refresh_expires_at,
        )
        access_token, access_expires_at = create_access_token(
            profile.user_id,
            session.id,
            data.id,
            settings=self._settings,
            now=now,
        )

        return LoginResult(
            user=profile.user,
            profile=profile,
            tokens=TokenPair(
                access_token=access_token,
                access_expires_at=access_expires_at,
                refresh_token=refresh_token,
                refresh_expires_at=refresh_expires_at,
            ),
            is_new=is_new,
        )

    async def get_user(self, user_id: int) -> User | None:
        """
        Load the authenticated user with the Telegram profile.

        Purpose:
            Resolve the current principal for authenticated profile endpoints.

        Args:
            user_id: Internal user identifier from verified access-token
                claims.

        Returns:
            User | None: The user with ``telegram_profile`` loaded, or ``None``
                when the user no longer exists.
        """
        return await self._repository.get_user_with_profile(user_id)
