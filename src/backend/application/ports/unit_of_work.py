"""Transaction boundary port shared by authentication and task-management services."""

from __future__ import annotations

from typing import Protocol


class UnitOfWork(Protocol):
    """
    Transaction boundary contract for application services.

    Purpose:
        Give services one explicit way to flush, commit, or roll back without
        depending on SQLAlchemy or on repository-specific transaction helpers.

    Notes:
        - Request-scoped services normally flush only; the request dependency
          owns the final commit.
        - Jobs that must persist intermediate steps call ``commit`` explicitly.
    """

    async def flush(self) -> None:
        """
        Flush pending changes without ending the transaction.

        Purpose:
            Persist in-place entity mutations performed by services while
            keeping the surrounding transaction open.

        Returns:
            None: The current unit of work is flushed.

        Notes:
            - Does not commit.
        """
        ...

    async def commit(self) -> None:
        """
        Commit the current transaction.

        Purpose:
            Persist an intermediate step such as a delivery claim or a
            generated series before continuing work that may fail later.

        Returns:
            None: The transaction is committed.

        Notes:
            - Also releases transaction-scoped row and advisory locks.
        """
        ...

    async def rollback(self) -> None:
        """
        Roll back the current transaction.

        Purpose:
            Discard a failed step so later work in the same job can continue
            from a clean unit of work.

        Returns:
            None: The transaction is rolled back.
        """
        ...
