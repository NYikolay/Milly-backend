from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from backend.auth.errors import AuthNotConfiguredError, RefreshTokenGraceError
from backend.auth.init_data import validate_init_data
from backend.auth.repository import AuthRepository
from backend.auth.tokens import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User

logger = structlog.get_logger(__name__)
_INVALID_REFRESH_TOKEN = "Invalid refresh token"


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
    """Coordinates Telegram login, refresh rotation, logout, and user lookup."""

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

    def _require_configuration(self) -> str:
        """
        Ensure the Telegram bot token and JWT secret are configured.

        Purpose:
            Fail before repository work when authentication cannot validate
            init data or issue access tokens.

        Returns:
            str: Raw Telegram bot token for init-data validation.

        Raises:
            AuthNotConfiguredError: If the Telegram bot token or JWT secret is
                not configured.
        """
        telegram_bot_token = self._settings.telegram_bot_token
        if telegram_bot_token is None or self._settings.auth_jwt_secret is None:
            raise AuthNotConfiguredError()
        return telegram_bot_token.get_secret_value()

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
        bot_token = self._require_configuration()

        now = self._clock()
        data = validate_init_data(
            raw_init_data,
            bot_token=bot_token,
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

    async def refresh(self, raw_refresh_token: str) -> TokenPair:
        """
        Rotate a refresh token and issue a new token pair.

        Purpose:
            Validate an opaque refresh token, detect reuse and expiry, then
            replace the stored session with a new rotation in the same family.

        Args:
            raw_refresh_token: Raw opaque refresh token presented by the
                client.

        Returns:
            TokenPair: Newly issued access and refresh credentials.

        Raises:
            AuthNotConfiguredError: If the Telegram bot token or JWT secret is
                not configured.
            RefreshTokenGraceError: If a just-rotated token is reused within
                the grace window; this is an ``UnauthorizedError`` subclass.
            UnauthorizedError: If the token is unknown, expired, reused outside
                the grace window, or its user or Telegram profile is missing.

        Notes:
            - Only the SHA-256 hash of the refresh token is sent to the
              repository.
            - Reuse outside grace and expiry revoke session state and commit
              before raising because the request unit of work would roll back.
            - Normal rotation revokes the old session and creates the new one
              in the same transaction; the dependency owns the commit.
            - Specific rejection reasons are logged server-side; clients always
              receive the generic message. Raw tokens are never logged.
        """
        self._require_configuration()
        now = self._clock()
        session = await self._repository.get_session_for_update(
            hash_refresh_token(raw_refresh_token)
        )
        if session is None:
            logger.warning("auth.refresh_rejected", reason="unknown")
            raise UnauthorizedError(_INVALID_REFRESH_TOKEN)

        if session.revoked_at is not None:
            grace = timedelta(seconds=self._settings.auth_refresh_reuse_grace_seconds)
            if now - session.revoked_at <= grace:
                logger.warning(
                    "auth.refresh_rejected",
                    reason="reuse_within_grace",
                    user_id=session.user_id,
                    session_id=session.id,
                )
                raise RefreshTokenGraceError(_INVALID_REFRESH_TOKEN)
            await self._repository.revoke_family_and_commit(session.family_id, now)
            logger.warning(
                "auth.refresh_rejected",
                reason="reuse_outside_grace",
                user_id=session.user_id,
                session_id=session.id,
                family_id=str(session.family_id),
            )
            raise UnauthorizedError(_INVALID_REFRESH_TOKEN)

        if session.expires_at <= now:
            await self._repository.revoke_session_and_commit(session, now)
            logger.warning(
                "auth.refresh_rejected",
                reason="expired",
                user_id=session.user_id,
                session_id=session.id,
            )
            raise UnauthorizedError(_INVALID_REFRESH_TOKEN)

        await self._repository.revoke_session(session, now)
        user = await self._repository.get_user_with_profile(session.user_id)
        if user is None:
            logger.warning(
                "auth.refresh_rejected",
                reason="user_missing",
                user_id=session.user_id,
                session_id=session.id,
            )
            raise UnauthorizedError(_INVALID_REFRESH_TOKEN)
        profile = user.telegram_profile
        if profile is None:
            logger.warning(
                "auth.refresh_rejected",
                reason="profile_missing",
                user_id=user.id,
                session_id=session.id,
            )
            raise UnauthorizedError(_INVALID_REFRESH_TOKEN)

        refresh_token = generate_refresh_token()
        refresh_expires_at = now + timedelta(seconds=self._settings.auth_refresh_ttl_seconds)
        new_session = await self._repository.create_session(
            user_id=user.id,
            family_id=session.family_id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            expires_at=refresh_expires_at,
        )
        access_token, access_expires_at = create_access_token(
            user.id,
            new_session.id,
            profile.telegram_id,
            settings=self._settings,
            now=now,
        )
        logger.info("auth.refresh_completed", user_id=user.id, session_id=new_session.id)
        return TokenPair(
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
        )

    async def logout(self, raw_refresh_token: str | None) -> None:
        """
        Revoke the refresh-token family for a client logout.

        Purpose:
            Terminate every rotation derived from the presented refresh token
            without failing when the token is absent or unknown.

        Args:
            raw_refresh_token: Raw refresh token presented by the client, or
                ``None`` when the client has no refresh token.

        Returns:
            None: The family is revoked in the current unit of work, or nothing
                happens for an absent or unknown token.

        Notes:
            - Logout is idempotent and does not reveal whether the token existed.
            - Only the SHA-256 hash of the refresh token is sent to the
              repository.
            - Does not commit; the request unit of work owns the transaction
              boundary.
            - Raw tokens are never logged.
        """
        if raw_refresh_token is None:
            return
        session = await self._repository.get_session_by_token_hash(
            hash_refresh_token(raw_refresh_token)
        )
        if session is None:
            return
        await self._repository.revoke_family(session.family_id, self._clock())
        logger.info(
            "auth.logout_completed",
            user_id=session.user_id,
            family_id=str(session.family_id),
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
