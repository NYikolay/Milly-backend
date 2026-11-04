from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.core.config import Settings


class Database:
    """Process-wide async database engine and session factory."""

    def __init__(self, settings: Settings) -> None:
        """
        Initialize the async engine and session factory.

        Purpose:
            Build the process-wide SQLAlchemy engine and the factory used to
            create request-scoped async sessions.

        Args:
            settings: Application settings providing the database URL, pool
                tuning, statement and pool timeouts, SQL echo, and connection
                application name.

        Notes:
            - Engine creation is lazy; no connection is opened until the first
              session or query needs one.
            - ``pool_timeout`` bounds waiting for a pooled connection;
              ``command_timeout`` and ``statement_timeout`` bound the runtime
              of a single statement.
            - ``dispose()`` releases the pool during application shutdown.
        """
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            echo=settings.sql_echo,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
            pool_timeout=settings.db_pool_timeout_seconds,
            connect_args={
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "command_timeout": settings.db_statement_timeout_ms / 1000,
                "server_settings": {
                    "application_name": settings.app_name,
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                },
            },
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """
        Open a session with an explicit commit/rollback boundary.

        Purpose:
            Provide a unit of work that commits on normal exit and rolls back
            when the caller raises.

        Returns:
            AsyncIterator[AsyncSession]: Context manager yielding the active
                session.

        Raises:
            BaseException: Propagates an exception from the block or from
                ``commit()``; rollback is attempted first.

        Notes:
            - Commits after the block succeeds; rolls back for any exception,
              including cancellation, and re-raises it.
            - The underlying session is always closed by the session factory's
              context manager, including after commit or rollback failures.
        """
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """
        Dispose the async engine and release its connection pool.

        Purpose:
            Close pooled connections when the application shuts down.

        Returns:
            None: This coroutine returns after the pool is disposed.

        Notes:
            - Does not open a new connection; safe to call when no query has
              been executed yet.
        """
        await self.engine.dispose()
