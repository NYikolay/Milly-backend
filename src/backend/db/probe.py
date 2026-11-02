import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger(__name__)


class DatabaseProbe:
    """Checks whether the database accepts a trivial query."""

    def __init__(self, engine: AsyncEngine) -> None:
        """
        Initialize the readiness probe.

        Purpose:
            Store the engine used to acquire a connection during each check.

        Args:
            engine: Async SQLAlchemy engine to probe.

        Notes:
            - Construction performs no I/O.
        """
        self.engine = engine

    async def check(self) -> bool:
        """
        Check database readiness with a trivial query.

        Purpose:
            Verify that the engine can acquire a connection and execute
            ``SELECT 1`` within the readiness time limit.

        Returns:
            bool: ``True`` when the query succeeds within two seconds,
                otherwise ``False``.

        Notes:
            - The query runs under a two-second ``asyncio.timeout``.
            - Failures are logged with a warning and converted to ``False``;
              they do not propagate to the caller.
        """
        try:
            async with asyncio.timeout(2):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.warning("db.readiness_failed", exc_info=True)
            return False
