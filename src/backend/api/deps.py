from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.auth.service import AuthService
from backend.application.ports.auth import TokenVerifier
from backend.application.task_management.labels import LabelService
from backend.application.task_management.palette import PaletteService
from backend.application.task_management.projects import ProjectService
from backend.application.task_management.series import SeriesService
from backend.application.task_management.task_mutations import TaskMutationService
from backend.application.task_management.tasks import TaskService
from backend.composition.auth import build_auth_service
from backend.composition.task_management import (
    TaskManagementServices,
    build_task_management_services,
)
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.db.probe import DatabaseProbe
from backend.db.session import Database
from backend.domain.auth.models import AccessTokenClaims
from backend.domain.entities import User


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


async def get_task_management_services(session: SessionDep) -> TaskManagementServices:
    """
    Build the task-management service graph for the current request.

    Purpose:
        Give every task-management route dependency one shared graph so all
        services use the same adapters, session, and unit of work.

    Args:
        session: Request-scoped database session.

    Returns:
        TaskManagementServices: Palette, project, label, series, task, and
            task-mutation services backed by the request session.

    Notes:
        - FastAPI caches this dependency per request, so derived service
          dependencies reuse one graph.
        - The services do not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return build_task_management_services(session)


TaskManagementServicesDep = Annotated[
    TaskManagementServices,
    Depends(get_task_management_services),
]


async def get_palette_service(services: TaskManagementServicesDep) -> PaletteService:
    """
    Resolve the palette service from the request task-management graph.

    Purpose:
        Keep palette routes on the same request graph as the other
        task-management routes.

    Args:
        services: Cached task-management services for the current request.

    Returns:
        PaletteService: Palette service backed by the request session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return services.palette


async def get_project_service(services: TaskManagementServicesDep) -> ProjectService:
    """
    Resolve the project service from the request task-management graph.

    Purpose:
        Keep project routes on the same request graph as the other
        task-management routes.

    Args:
        services: Cached task-management services for the current request.

    Returns:
        ProjectService: Project service backed by the request session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return services.projects


async def get_label_service(services: TaskManagementServicesDep) -> LabelService:
    """
    Resolve the label service from the request task-management graph.

    Purpose:
        Keep label routes on the same request graph as the other
        task-management routes.

    Args:
        services: Cached task-management services for the current request.

    Returns:
        LabelService: Label service backed by the request session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return services.labels


PaletteServiceDep = Annotated[PaletteService, Depends(get_palette_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
LabelServiceDep = Annotated[LabelService, Depends(get_label_service)]


async def get_series_service(services: TaskManagementServicesDep) -> SeriesService:
    """
    Resolve the recurring-series service from the request task-management graph.

    Purpose:
        Keep series routes on the same request graph as the other
        task-management routes.

    Args:
        services: Cached task-management services for the current request.

    Returns:
        SeriesService: Series service backed by the request session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return services.series


async def get_task_service(services: TaskManagementServicesDep) -> TaskService:
    """
    Resolve the task service from the request task-management graph.

    Purpose:
        Keep task routes on the same request graph as the other
        task-management routes.

    Args:
        services: Cached task-management services for the current request.

    Returns:
        TaskService: Task service backed by the request session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return services.tasks


SeriesServiceDep = Annotated[SeriesService, Depends(get_series_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]


async def get_task_mutation_service(
    services: TaskManagementServicesDep,
) -> TaskMutationService:
    """
    Resolve the task mutation service from the request task-management graph.

    Purpose:
        Keep task update, reopen, and delete routes on the same request graph
        as the other task-management routes.

    Args:
        services: Cached task-management services for the current request.

    Returns:
        TaskMutationService: Task mutation service backed by the request
            session.

    Notes:
        - The service does not commit; ``SessionDep`` owns the transaction
          boundary.
    """
    return services.task_mutations


TaskMutationServiceDep = Annotated[TaskMutationService, Depends(get_task_mutation_service)]


async def get_auth_service(request: Request, session: SessionDep) -> AuthService:
    """
    Build the authentication service for the current request.

    Purpose:
        Build the authentication service through the composition root with the
        application settings and the request-scoped session used by login,
        refresh, logout, and the ``/me`` current-user lookup.

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
    return build_auth_service(session, settings)


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
        verifying the JWT through the application token verifier.

    Args:
        request: Incoming request carrying the application token verifier.
        credentials: Parsed bearer credentials, or ``None`` when the header is
            missing or malformed.

    Returns:
        AccessTokenClaims: Verified identity and session claims.

    Raises:
        AuthNotConfiguredError: If no JWT secret is configured.
        UnauthorizedError: If the Authorization header is missing or the token
            fails verification.

    Notes:
        - ``HTTPBearer(auto_error=False)`` lets the dependency raise the project
          error so the unified envelope and Bearer challenge are preserved.
        - The verifier is built once during application assembly through the
          composition root and stored on ``app.state.token_verifier``.
        - Raw tokens are never logged.
    """
    if credentials is None:
        raise UnauthorizedError("Authentication required")
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier.verify_access_token(credentials.credentials)


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
        User: The domain user entity with ``telegram_profile`` loaded; never an
            ORM row.

    Raises:
        UnauthorizedError: If the user no longer exists or has no Telegram
            profile.
    """
    user = await service.get_user(claims.user_id)
    if user is None or user.telegram_profile is None:
        raise UnauthorizedError("Authentication required")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
