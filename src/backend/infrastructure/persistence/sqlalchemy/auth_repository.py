"""SQLAlchemy implementation of the authentication persistence port."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.domain.auth.models import TelegramIdentity, TelegramUser
from backend.domain.entities import AuthSession, TelegramProfile, User
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_profile,
    auth_session_to_entity,
    profile_to_entity,
    user_to_entity,
)
from backend.models.auth_session import AuthSession as AuthSessionRow
from backend.models.telegram_profile import TelegramProfile as TelegramProfileRow
from backend.models.user import User as UserRow


class SqlAlchemyAuthRepository:
    """SQLAlchemy implementation of the authentication persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the repository with a request-scoped session.

        Purpose:
            Bind authentication persistence operations to the caller's unit of
            work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The repository never closes the session or commits; the caller
              controls the transaction boundary.
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
            - Fetches the profile and its user in one joined SELECT.
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(UserRow, TelegramProfileRow)
            .join(TelegramProfileRow, TelegramProfileRow.user_id == UserRow.id)
            .where(TelegramProfileRow.telegram_id == telegram_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        user_row, profile_row = row
        user = user_to_entity(user_row)
        profile = profile_to_entity(profile_row)
        user.telegram_profile = profile
        return TelegramIdentity(user=user, profile=profile)

    async def create_user_with_profile(self, data: TelegramUser) -> TelegramIdentity:
        """
        Create a user and its Telegram profile.

        Purpose:
            Persist a first-time Telegram identity with all validated profile
            fields.

        Args:
            data: Validated Telegram user payload.

        Returns:
            TelegramIdentity: The persisted user/profile pair with generated
                identifiers.

        Notes:
            - Inserts and flushes the user before the profile so the generated
              ``user_id`` can be assigned.
            - Flushes both records to populate identifiers; it does not commit.
            - The transient domain ``id=0`` is never copied into identity
              columns.
        """
        user_row = UserRow()
        self._session.add(user_row)
        await self._session.flush()
        profile_row = TelegramProfileRow(
            user_id=user_row.id,
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
        self._session.add(profile_row)
        await self._session.flush()
        user = user_to_entity(user_row)
        profile = profile_to_entity(profile_row)
        user.telegram_profile = profile
        return TelegramIdentity(user=user, profile=profile)

    async def update_profile(
        self,
        profile: TelegramProfile,
        data: TelegramUser,
    ) -> None:
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

        Raises:
            LookupError: If the profile row is not present in the session.

        Notes:
            - Only ``first_name``, ``last_name``, ``username``,
              ``language_code``, ``is_premium``, ``added_to_attachment_menu``,
              ``allows_write_to_pm``, and ``photo_url`` are changed.
            - ``telegram_id``, ``user_id``, and ``is_bot`` remain untouched.
            - Flushes but does not commit.
        """
        row = await self._session.get(TelegramProfileRow, profile.id)
        if row is None:
            raise LookupError(f"Telegram profile {profile.id} is not present in the session")
        profile.first_name = data.first_name
        profile.last_name = data.last_name
        profile.username = data.username
        profile.language_code = data.language_code
        profile.is_premium = data.is_premium
        profile.added_to_attachment_menu = data.added_to_attachment_menu
        profile.allows_write_to_pm = data.allows_write_to_pm
        profile.photo_url = data.photo_url
        apply_profile(profile, row)
        await self._session.flush()

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
        await self._session.execute(
            update(AuthSessionRow)
            .where(
                AuthSessionRow.user_id == user_id,
                AuthSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )

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
        session = AuthSessionRow(
            user_id=user_id,
            family_id=family_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
        self._session.add(session)
        await self._session.flush()
        return auth_session_to_entity(session)

    async def get_telegram_id_for_session(self, refresh_token_hash: str) -> int | None:
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
            - Uses a non-locking SELECT joining sessions to Telegram profiles.
            - Does not commit.
        """
        result = await self._session.execute(
            select(TelegramProfileRow.telegram_id)
            .join(AuthSessionRow, AuthSessionRow.user_id == TelegramProfileRow.user_id)
            .where(AuthSessionRow.refresh_token_hash == refresh_token_hash)
        )
        return result.scalar_one_or_none()

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
            - Does not commit.
        """
        result = await self._session.execute(
            select(AuthSessionRow)
            .where(AuthSessionRow.refresh_token_hash == refresh_token_hash)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return auth_session_to_entity(row)

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
            - Updates the session row by primary key, matching the previous
              ORM-flush behavior for an already locked entity.
            - Flushes but does not commit; callers that must persist before
              raising commit through ``UnitOfWork``.
        """
        session.revoked_at = revoked_at
        await self._session.execute(
            update(AuthSessionRow)
            .where(AuthSessionRow.id == session.id)
            .values(revoked_at=revoked_at)
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

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
        await self._session.execute(
            update(AuthSessionRow)
            .where(
                AuthSessionRow.family_id == family_id,
                AuthSessionRow.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )

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
            - Eagerly loads the profile with a second batched SELECT.
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(UserRow)
            .where(UserRow.id == user_id)
            .options(selectinload(UserRow.telegram_profile))
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        user = user_to_entity(row)
        if row.telegram_profile is not None:
            user.telegram_profile = profile_to_entity(row.telegram_profile)
        return user

    async def update_user_timezone(self, user_id: int, timezone: str) -> User | None:
        """
        Update the IANA timezone of a user.

        Purpose:
            Persist a validated profile timezone change and return the
            refreshed user with its Telegram profile.

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
        await self._session.execute(
            update(UserRow).where(UserRow.id == user_id).values(timezone=timezone)
        )
        return await self.get_user_with_profile(user_id)
