from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.repository import SqlAlchemyAuthRepository
from backend.auth.service import AuthService
from backend.auth.tokens import AccessTokenClaims, decode_access_token
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.db.probe import DatabaseProbe
from backend.db.session import Database
from backend.models.user import User


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


async def get_auth_service(request: Request, session: SessionDep) -> AuthService:
    """
    Build the authentication service for the current request.

    Purpose:
        Bind the SQLAlchemy auth repository and application settings to the
        request-scoped session used by login, refresh, and logout.

    Args:
        request: Incoming request carrying the application settings.
        session: Request-scoped database session.

    Returns:
        AuthService: Authentication service backed by the request session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    settings: Settings = request.app.state.settings
    return AuthService(SqlAlchemyAuthRepository(session), settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
security = HTTPBearer(auto_error=False)


async def get_current_access(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> AccessTokenClaims:
    """
    Verify the bearer access token and return its claims.

    Purpose:
        Authenticate protected endpoints without opening a database session by
        decoding the JWT with the application settings.

    Args:
        request: Incoming request carrying the application settings.
        credentials: Parsed bearer credentials, or ``None`` when the header is
            missing or malformed.

    Returns:
        AccessTokenClaims: Verified identity and session claims.

    Raises:
        UnauthorizedError: If the Authorization header is missing or the token
            fails verification.

    Notes:
        - ``HTTPBearer(auto_error=False)`` lets the dependency raise the project
          error so the unified envelope and Bearer challenge are preserved.
        - Raw tokens are never logged.
    """
    if credentials is None:
        raise UnauthorizedError("Authentication required")
    settings: Settings = request.app.state.settings
    return decode_access_token(credentials.credentials, settings=settings)


CurrentAccessDep = Annotated[AccessTokenClaims, Depends(get_current_access)]


async def get_current_user(
    claims: CurrentAccessDep,
    service: AuthServiceDep,
) -> User:
    """
    Load the authenticated user with the Telegram profile.

    Purpose:
        Resolve the current principal for endpoints that need persisted profile
        data rather than only token claims.

    Args:
        claims: Verified access-token claims.
        service: Authentication service used to load the user.

    Returns:
        User: The user with ``telegram_profile`` loaded.

    Raises:
        UnauthorizedError: If the user no longer exists or has no Telegram
            profile.
    """
    user = await service.get_user(claims.user_id)
    if user is None or user.telegram_profile is None:
        raise UnauthorizedError("Authentication required")
    return user


SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
DatabaseProbeDep = Annotated[DatabaseProbe, Depends(get_database_probe)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
