from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.probe import DatabaseProbe
from backend.db.session import Database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """
    Yield a request-scoped database session.

    Purpose:
        Provide the session dependency backed by the application-wide
        ``Database`` stored on ``request.app.state``.

    Args:
        request: Incoming request carrying the application state.

    Returns:
        AsyncIterator[AsyncSession]: Context manager yielding the session for
            the current request.

    Notes:
        - Commit, rollback, and close are delegated to ``Database.session()``.
        - ``SessionDep`` binds this dependency with ``scope="function"``, so the
          commit runs after the path operation returns and before the response
          is sent.
    """
    database: Database = request.app.state.db
    async with database.session() as session:
        yield session


def get_database_probe(request: Request) -> DatabaseProbe:
    """
    Build a readiness probe for the application database.

    Purpose:
        Provide a probe bound to the engine owned by the application-wide
        ``Database``.

    Args:
        request: Incoming request carrying the application state.

    Returns:
        DatabaseProbe: Probe configured with the application's async engine.

    Notes:
        - Reads ``request.app.state.db``; no connection is attempted until
          ``DatabaseProbe.check()`` is called.
    """
    database: Database = request.app.state.db
    return DatabaseProbe(database.engine)


SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
DatabaseProbeDep = Annotated[DatabaseProbe, Depends(get_database_probe)]
