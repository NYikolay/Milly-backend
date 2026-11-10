"""SQLAlchemy implementation of the application transaction boundary."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class SqlAlchemyUnitOfWork:
    """
    Transaction boundary adapter backed by one SQLAlchemy session.

    Purpose:
        Give application services an ORM-independent way to flush, commit, or
        roll back the current session.

    Args:
        session: Request- or job-scoped async session owning the transaction.

    Notes:
        - All operations delegate directly to the session and add no transaction
          management of their own.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Bind the unit of work to an existing session.

        Purpose:
            Reuse the caller-owned request- or job-scoped session instead of
            opening another transaction.

        Args:
            session: Async session owning the transaction boundary.
        """
        self._session = session

    async def flush(self) -> None:
        """
        Flush pending session changes without ending the transaction.

        Purpose:
            Persist in-place mutations while keeping the transaction open.

        Returns:
            None: The session is flushed.

        Notes:
            - Does not commit.
        """
        await self._session.flush()

    async def commit(self) -> None:
        """
        Commit the current session transaction.

        Purpose:
            Persist an intermediate step before continuing work that may fail
            later.

        Returns:
            None: The transaction is committed.

        Notes:
            - Also releases transaction-scoped row and advisory locks.
        """
        await self._session.commit()

    async def rollback(self) -> None:
        """
        Roll back the current session transaction.

        Purpose:
            Discard a failed step so later work can continue from a clean unit
            of work.

        Returns:
            None: The transaction is rolled back.
        """
        await self._session.rollback()
