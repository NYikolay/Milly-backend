"""Authentication persistence, validation, and token-issuing ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.application.ports.unit_of_work import UnitOfWork as UnitOfWork
from backend.domain.auth.models import (
    AccessTokenClaims,
    TelegramIdentity,
    TelegramUser,
)
from backend.domain.entities import AuthSession, TelegramProfile, User


class AuthRepository(Protocol):
    """
    Persistence contract for Telegram authentication operations.

    Purpose:
        Expose identity, session, and user-profile operations using domain
        entities while keeping the auth service independent of SQLAlchemy.

    Notes:
        - Implementations flush pending changes but never commit; the
          AuthService commits through ``UnitOfWork`` when a revocation must
          survive a subsequent error.
        - Advisory locks and row locks are held until the transaction ends.
    """

    async def acquire_telegram_lock(self, telegram_id: int) -> None:
        """
        Acquire a transaction-scoped advisory lock for a Telegram user.

        Purpose:
            Serialize concurrent logins of the same Telegram identity so only
            one request creates the user and profile.

        Args:
            telegram_id: Telegram user identifier to lock.

        Returns:
            None: The lock is held until the surrounding transaction ends.

        Notes:
            - The lock is released automatically on commit or rollback.
        """
        ...

    async def get_identity_by_telegram_id(
        self,
        telegram_id: int,
    ) -> TelegramIdentity | None:
        """
        Load an application user together with its Telegram profile.

        Purpose:
            Resolve an existing login identity after the advisory lock is held.

        Args:
            telegram_id: Telegram user identifier to look up.

        Returns:
            TelegramIdentity | None: The matching user/profile pair, or
                ``None`` when the identity has not logged in before.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def create_user_with_profile(self, data: TelegramUser) -> TelegramIdentity:
        """
        Create a user and its Telegram profile.

        Purpose:
            Persist the application user and the validated Telegram identity in
            one unit of work.

        Args:
            data: Validated Telegram user payload.

        Returns:
            TelegramIdentity: The persisted user/profile pair with generated
                identifiers.

        Notes:
            - Flushes both records but does not commit.
        """
        ...

    async def update_profile(
        self,
        profile: TelegramProfile,
        data: TelegramUser,
    ) -> None:
        """
        Refresh mutable Telegram profile fields from validated init data.

        Purpose:
            Keep display and premium metadata in sync on every login without
            changing identity columns.

        Args:
            profile: Existing profile to update.
            data: Fresh validated Telegram user payload.

        Returns:
            None: The profile is updated in the current unit of work.

        Notes:
            - Only ``first_name``, ``last_name``, ``username``,
              ``language_code``, ``is_premium``, ``added_to_attachment_menu``,
              ``allows_write_to_pm``, and ``photo_url`` are changed.
            - ``telegram_id``, ``user_id``, and ``is_bot`` remain untouched.
            - Flushes but does not commit.
        """
        ...

    async def revoke_user_sessions(self, user_id: int, revoked_at: datetime) -> None:
        """
        Revoke every active session of one user.

        Purpose:
            Enforce a single active refresh session per user on a new login.

        Args:
            user_id: Internal user identifier whose sessions are revoked.
            revoked_at: Revocation timestamp.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Only sessions whose ``revoked_at`` is unset are updated.
            - Does not commit; the caller owns the transaction boundary.
        """
        ...

    async def create_session(
        self,
        *,
        user_id: int,
        family_id: UUID,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> AuthSession:
        """
        Persist a refresh-token session.

        Purpose:
            Record a login session whose refresh token is stored only as a
            hash.

        Args:
            user_id: Internal user identifier owning the session.
            family_id: Refresh-token family identifier shared by rotations.
            refresh_token_hash: SHA-256 hex digest of the raw refresh token.
            expires_at: Session expiry time.

        Returns:
            AuthSession: The flushed session with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def get_telegram_id_for_session(
        self,
        refresh_token_hash: str,
    ) -> int | None:
        """
        Resolve the Telegram identity owning a refresh-token session.

        Purpose:
            Read the Telegram id needed to acquire the login advisory lock
            before locking the session row during refresh.

        Args:
            refresh_token_hash: SHA-256 hex digest of the raw refresh token.

        Returns:
            int | None: The Telegram id linked to the session user, or ``None``
                when no session matches the hash.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_session_for_update(
        self,
        refresh_token_hash: str,
    ) -> AuthSession | None:
        """
        Load a session by refresh-token hash while locking its row.

        Purpose:
            Serialize concurrent refresh and logout requests for one token.

        Args:
            refresh_token_hash: SHA-256 hex digest of the raw refresh token.

        Returns:
            AuthSession | None: The locked matching session, or ``None``.

        Notes:
            - The row lock is held until the surrounding transaction ends.
        """
        ...

    async def revoke_session(self, session: AuthSession, revoked_at: datetime) -> None:
        """
        Mark one session as revoked.

        Purpose:
            Invalidate a refresh token during rotation, expiry handling, or
            reuse detection.

        Args:
            session: Session to revoke.
            revoked_at: Revocation timestamp.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Does not commit; callers that must persist before raising commit
              through ``UnitOfWork``.
        """
        ...

    async def revoke_family(self, family_id: UUID, revoked_at: datetime) -> None:
        """
        Revoke all active sessions in a refresh-token family.

        Purpose:
            Terminate every rotation of a compromised or logged-out refresh
            token family.

        Args:
            family_id: Refresh-token family identifier.
            revoked_at: Revocation timestamp.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Only sessions whose ``revoked_at`` is unset are updated.
            - Does not commit; callers that must persist before raising commit
              through ``UnitOfWork``.
        """
        ...

    async def get_user_with_profile(self, user_id: int) -> User | None:
        """
        Load a user together with the Telegram profile.

        Purpose:
            Resolve the authenticated principal for ``/auth/me`` responses.

        Args:
            user_id: Internal user identifier.

        Returns:
            User | None: The matching user with ``telegram_profile`` loaded, or
                ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def update_user_timezone(self, user_id: int, timezone: str) -> User | None:
        """
        Update the IANA timezone of a user.

        Purpose:
            Persist a validated profile timezone change.

        Args:
            user_id: Internal user identifier.
            timezone: Validated IANA timezone name.

        Returns:
            User | None: The updated user with ``telegram_profile`` loaded, or
                ``None`` when the user no longer exists.

        Notes:
            - Executes the update in the current unit of work and does not
              commit; the caller owns the transaction boundary.
        """
        ...


class InitDataValidator(Protocol):
    """
    Validation contract for signed Telegram Mini App init data.

    Purpose:
        Keep the auth service independent of the Pydantic-based validator
        implementation.

    Notes:
        - Implementations raise ``UnauthorizedError`` for any invalid payload.
        - Raw init data must never be logged or included in error details.
    """

    def validate(self, raw_init_data: str) -> TelegramUser:
        """
        Validate signed Telegram init data and return its user.

        Purpose:
            Authenticate Mini App launches by verifying that Telegram signed
            the payload and that it is fresh and describes a regular user.

        Args:
            raw_init_data: Raw ``Telegram.WebApp.initData`` query string.

        Returns:
            TelegramUser: Validated user payload from the init data.

        Raises:
            UnauthorizedError: If the payload is empty, malformed, duplicated,
                incorrectly signed, stale, or contains a bot user.

        Notes:
            - Every rejection uses the same opaque message so failed checks are
              not disclosed to the client.
        """
        ...


class TokenIssuer(Protocol):
    """
    Access-token issuance contract for the auth service.

    Purpose:
        Keep JWT formatting and signing out of the application layer.

    Notes:
        - Implementations must return the exact expiry instant stored in the
          token claims.
    """

    def issue_access_token(
        self,
        user_id: int,
        session_id: int,
        telegram_id: int,
        *,
        now: datetime,
    ) -> tuple[str, datetime]:
        """
        Issue a signed access token.

        Purpose:
            Produce an access token for a newly created or refreshed session
            using an explicit clock value.

        Args:
            user_id: Internal user identifier.
            session_id: Auth session identifier.
            telegram_id: Linked Telegram user identifier.
            now: Current instant used to compute issue and expiry times.

        Returns:
            tuple[str, datetime]: Encoded access token and its expiry instant.

        Notes:
            - Does not persist anything; the caller stores the session.
        """
        ...


class TokenVerifier(Protocol):
    """
    Access-token verification contract for request authentication.

    Purpose:
        Let the API verify bearer tokens through the application boundary
        without importing the JWT adapter.

    Notes:
        - Implementations return validated domain claims and raise
          ``UnauthorizedError`` for every invalid token.
        - Raw tokens must never be logged or included in error details.
    """

    def verify_access_token(self, raw_token: str) -> AccessTokenClaims:
        """
        Verify an encoded access token and return its claims.

        Purpose:
            Validate the signature, issuer, required claims, token type, and
            configured time bounds before a request is authenticated.

        Args:
            raw_token: Encoded bearer access token.

        Returns:
            AccessTokenClaims: Validated identity and session claims.

        Raises:
            AuthNotConfiguredError: If no JWT secret is configured.
            UnauthorizedError: If the token is malformed, incorrectly signed,
                expired beyond the leeway, issued too far in the future, has
                the wrong issuer or audience, or contains invalid claims.

        Notes:
            - Implementations must not persist anything or open a database
              session.
        """
        ...
