"""SQLAlchemy implementation of the account persistence port."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_profile import TelegramProfile as TelegramProfileRow
from backend.models.user import User as UserRow


class SqlAlchemyAccountRepository:
    """SQLAlchemy implementation of the account persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the account adapter with a request-scoped session.

        Purpose:
            Bind account reads to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

    async def get_user_timezone(self, user_id: int) -> str | None:
        """
        Load the IANA timezone of a user.

        Purpose:
            Convert a local task-list date into UTC day bounds and compute
            scheduled reminder slots without depending on the auth layer.

        Args:
            user_id: Owner identifier.

        Returns:
            str | None: The stored timezone name, or ``None`` when the user is
                missing.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(select(UserRow.timezone).where(UserRow.id == user_id))
        return result.scalar_one_or_none()

    async def get_telegram_id(self, user_id: int) -> int | None:
        """
        Load the Telegram chat identifier of a user.

        Purpose:
            Address reminder deliveries to the external Telegram identity
            instead of the internal application user identifier.

        Args:
            user_id: Internal user identifier.

        Returns:
            int | None: The linked ``telegram_profiles.telegram_id``, or
                ``None`` when the user has no Telegram profile.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TelegramProfileRow.telegram_id).where(TelegramProfileRow.user_id == user_id)
        )
        return result.scalar_one_or_none()
