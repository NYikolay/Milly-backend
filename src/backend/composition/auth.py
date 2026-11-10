"""Composition factory for the authentication application service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.auth.service import AuthService
from backend.core.config import Settings
from backend.domain.auth.models import AuthSettings
from backend.infrastructure.auth.init_data import PydanticInitDataValidator
from backend.infrastructure.auth.tokens import JwtTokenIssuer
from backend.infrastructure.persistence.sqlalchemy.auth_repository import (
    SqlAlchemyAuthRepository,
)
from backend.infrastructure.persistence.sqlalchemy.unit_of_work import (
    SqlAlchemyUnitOfWork,
)

__all__ = ["build_auth_service"]


def _auth_settings(settings: Settings) -> AuthSettings:
    """
    Convert application settings into the plain auth configuration DTO.

    Purpose:
        Keep the application layer independent of Pydantic settings while
        preserving every authentication value.

    Args:
        settings: Application settings with Telegram and JWT configuration.

    Returns:
        AuthSettings: Plain settings consumed by the auth service.

    Notes:
        - Secret values are unwrapped here so the application layer never
          depends on ``SecretStr``.
        - Does not mutate or retain the supplied settings object.
    """
    return AuthSettings(
        telegram_bot_token=(
            settings.telegram_bot_token.get_secret_value()
            if settings.telegram_bot_token is not None
            else None
        ),
        auth_jwt_secret=(
            settings.auth_jwt_secret.get_secret_value()
            if settings.auth_jwt_secret is not None
            else None
        ),
        auth_jwt_issuer=settings.auth_jwt_issuer,
        auth_jwt_leeway_seconds=settings.auth_jwt_leeway_seconds,
        telegram_init_data_max_age_seconds=settings.telegram_init_data_max_age_seconds,
        telegram_init_data_max_future_skew_seconds=(
            settings.telegram_init_data_max_future_skew_seconds
        ),
        auth_access_ttl_seconds=settings.auth_access_ttl_seconds,
        auth_refresh_ttl_seconds=settings.auth_refresh_ttl_seconds,
        auth_refresh_reuse_grace_seconds=settings.auth_refresh_reuse_grace_seconds,
    )


def build_auth_service(
    session: AsyncSession,
    settings: Settings,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AuthService:
    """
    Build the authentication service over one session and application settings.

    Purpose:
        Give API entrypoints one place that maps settings and selects the
        init-data, token, persistence, and transaction adapters.

    Args:
        session: Request-scoped async session owning the transaction.
        settings: Application settings with Telegram and JWT configuration.
        clock: Current-time provider. Defaults to the service UTC clock.

    Returns:
        AuthService: Authentication service wired over the supplied session.

    Notes:
        - The auth repository and unit of work share the supplied session.
        - The validator and token issuer read the original settings object.
        - Does not commit, flush, or open a transaction.
    """
    return AuthService(
        SqlAlchemyAuthRepository(session),
        SqlAlchemyUnitOfWork(session),
        PydanticInitDataValidator(settings),
        JwtTokenIssuer(settings),
        _auth_settings(settings),
        clock,
    )
