from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.auth.init_data import TelegramUser
from backend.models.auth_session import AuthSession
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


class AuthRepository(Protocol):
    """Persistence contract for Telegram authentication operations."""

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

    async def get_profile_by_telegram_id(self, telegram_id: int) -> TelegramProfile | None:
        """
        Load a Telegram profile together with its owning user.

        Purpose:
            Resolve an existing login identity after the advisory lock is held.

        Args:
            telegram_id: Telegram user identifier to look up.

        Returns:
            TelegramProfile | None: The matching profile with ``user`` loaded,
                or ``None`` when the identity has not logged in before.
        """
        ...

    async def create_user_with_profile(self, data: TelegramUser) -> TelegramProfile:
        """
        Create a user and its Telegram profile.

        Purpose:
            Persist the application user and the validated Telegram identity in
            one unit of work.

        Args:
            data: Validated Telegram user payload.

        Returns:
            TelegramProfile: The persisted profile linked to the new user.

        Notes:
            - Flushes both records so generated identifiers are available; it
              does not commit.
        """
        ...

    async def update_profile(self, profile: TelegramProfile, data: TelegramUser) -> None:
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

    async def get_session_for_update(self, refresh_token_hash: str) -> AuthSession | None:
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

    async def get_session_by_token_hash(self, refresh_token_hash: str) -> AuthSession | None:
        """
        Load a session by refresh-token hash without locking.

        Purpose:
            Resolve a session for read-only logout handling.

        Args:
            refresh_token_hash: SHA-256 hex digest of the raw refresh token.

        Returns:
            AuthSession | None: The matching session, or ``None``.
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
            - Does not commit; callers that must persist before raising use
              ``revoke_session_and_commit``.
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
            - Does not commit; use ``revoke_family_and_commit`` when the
              revocation must survive a subsequent error.
        """
        ...

    async def revoke_session_and_commit(
        self,
        session: AuthSession,
        revoked_at: datetime,
    ) -> None:
        """
        Revoke one session and commit immediately.

        Purpose:
            Persist a revocation on the refresh error path where the caller
            raises afterwards and the request unit of work would roll back.

        Args:
            session: Session to revoke.
            revoked_at: Revocation timestamp.

        Returns:
            None: The revocation is committed.

        Notes:
            - This is an explicit commit-on-error helper; normal login and
              logout flows commit at the dependency boundary.
        """
        ...

    async def revoke_family_and_commit(self, family_id: UUID, revoked_at: datetime) -> None:
        """
        Revoke a refresh-token family and commit immediately.

        Purpose:
            Persist reuse-detection revocation before the caller raises ``401``.

        Args:
            family_id: Refresh-token family identifier.
            revoked_at: Revocation timestamp.

        Returns:
            None: The revocation is committed.

        Notes:
            - This is an explicit commit-on-error helper; normal login and
              logout flows commit at the dependency boundary.
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
        """
        ...


class SqlAlchemyAuthRepository:
    """SQLAlchemy implementation of the authentication repository contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the repository with a request-scoped session.

        Purpose:
            Bind authentication persistence operations to the caller's unit of
            work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The repository never closes the session; the caller controls the
              transaction boundary.
        """
        self._session = session

    async def acquire_telegram_lock(self, telegram_id: int) -> None:
        """
        Acquire a transaction-scoped PostgreSQL advisory lock.

        Purpose:
            Prevent concurrent logins from creating duplicate users for the
            same Telegram identity.

        Args:
            telegram_id: Telegram user identifier to lock.

        Returns:
            None: The lock is held until the transaction ends.

        Notes:
            - PostgreSQL releases the lock automatically on commit or rollback.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:telegram_id)"),
            {"telegram_id": telegram_id},
        )

    async def get_profile_by_telegram_id(self, telegram_id: int) -> TelegramProfile | None:
        """
        Load a Telegram profile together with its owning user.

        Purpose:
            Resolve an existing login identity after the advisory lock is held.

        Args:
            telegram_id: Telegram user identifier to look up.

        Returns:
            TelegramProfile | None: The matching profile with ``user`` eagerly
                loaded, or ``None``.

        Notes:
            - Does not lock the row; the advisory lock serializes logins.
        """
        result = await self._session.execute(
            select(TelegramProfile)
            .where(TelegramProfile.telegram_id == telegram_id)
            .options(selectinload(TelegramProfile.user))
        )
        return result.scalar_one_or_none()

    async def create_user_with_profile(self, data: TelegramUser) -> TelegramProfile:
        """
        Create a user and its Telegram profile.

        Purpose:
            Persist a first-time Telegram identity with all validated profile
            fields.

        Args:
            data: Validated Telegram user payload.

        Returns:
            TelegramProfile: The flushed profile linked to the new user.

        Notes:
            - Flushes both records to populate generated identifiers; it does
              not commit.
            - ``user_id`` is populated through the relationship.
        """
        user = User()
        self._session.add(user)
        await self._session.flush()
        profile = TelegramProfile(
            user=user,
            telegram_id=data.id,
            is_bot=data.is_bot,
            first_name=data.first_name,
            last_name=data.last_name,
            username=data.username,
            language_code=data.language_code,
            is_premium=data.is_premium,
            added_to_attachment_menu=data.added_to_attachment_menu,
            allows_write_to_pm=data.allows_write_to_pm,
            photo_url=data.photo_url,
        )
        self._session.add(profile)
        await self._session.flush()
        return profile

    async def update_profile(self, profile: TelegramProfile, data: TelegramUser) -> None:
        """
        Refresh mutable Telegram profile fields from validated init data.

        Purpose:
            Keep display and premium metadata current on every login while
            preserving identity columns.

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
        """
        profile.first_name = data.first_name
        profile.last_name = data.last_name
        profile.username = data.username
        profile.language_code = data.language_code
        profile.is_premium = data.is_premium
        profile.added_to_attachment_menu = data.added_to_attachment_menu
        profile.allows_write_to_pm = data.allows_write_to_pm
        profile.photo_url = data.photo_url

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
        session = AuthSession(
            user_id=user_id,
            family_id=family_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
        self._session.add(session)
        await self._session.flush()
        return session

    async def get_session_for_update(self, refresh_token_hash: str) -> AuthSession | None:
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
        result = await self._session.execute(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == refresh_token_hash)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_session_by_token_hash(self, refresh_token_hash: str) -> AuthSession | None:
        """
        Load a session by refresh-token hash without locking.

        Purpose:
            Resolve a session for read-only logout handling.

        Args:
            refresh_token_hash: SHA-256 hex digest of the raw refresh token.

        Returns:
            AuthSession | None: The matching session, or ``None``.
        """
        result = await self._session.execute(
            select(AuthSession).where(AuthSession.refresh_token_hash == refresh_token_hash)
        )
        return result.scalar_one_or_none()

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
            - Does not commit; callers that must persist before raising use
              ``revoke_session_and_commit``.
        """
        session.revoked_at = revoked_at

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
            - Does not commit; use ``revoke_family_and_commit`` when the
              revocation must survive a subsequent error.
        """
        await self._session.execute(
            update(AuthSession)
            .where(
                AuthSession.family_id == family_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )

    async def revoke_session_and_commit(
        self,
        session: AuthSession,
        revoked_at: datetime,
    ) -> None:
        """
        Revoke one session and commit immediately.

        Purpose:
            Persist a revocation on the refresh error path where the caller
            raises afterwards and the request unit of work would roll back.

        Args:
            session: Session to revoke.
            revoked_at: Revocation timestamp.

        Returns:
            None: The revocation is committed.

        Notes:
            - This is an explicit commit-on-error helper; normal login and
              logout flows commit at the dependency boundary.
        """
        await self.revoke_session(session, revoked_at)
        await self._session.commit()

    async def revoke_family_and_commit(self, family_id: UUID, revoked_at: datetime) -> None:
        """
        Revoke a refresh-token family and commit immediately.

        Purpose:
            Persist reuse-detection revocation before the caller raises ``401``.

        Args:
            family_id: Refresh-token family identifier.
            revoked_at: Revocation timestamp.

        Returns:
            None: The revocation is committed.

        Notes:
            - This is an explicit commit-on-error helper; normal login and
              logout flows commit at the dependency boundary.
        """
        await self.revoke_family(family_id, revoked_at)
        await self._session.commit()

    async def get_user_with_profile(self, user_id: int) -> User | None:
        """
        Load a user together with the Telegram profile.

        Purpose:
            Resolve the authenticated principal for ``/auth/me`` responses.

        Args:
            user_id: Internal user identifier.

        Returns:
            User | None: The matching user with ``telegram_profile`` eagerly
                loaded, or ``None``.
        """
        result = await self._session.execute(
            select(User).where(User.id == user_id).options(selectinload(User.telegram_profile))
        )
        return result.scalar_one_or_none()
