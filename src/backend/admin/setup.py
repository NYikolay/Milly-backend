from __future__ import annotations

import structlog
from fastapi import FastAPI
from sqladmin import Admin

from backend.admin.auth import AdminAuth
from backend.admin.views import AuthSessionAdmin, TelegramProfileAdmin, UserAdmin
from backend.core.config import Settings
from backend.db.session import Database

logger = structlog.get_logger(__name__)


def setup_admin(
    app: FastAPI,
    database: Database,
    settings: Settings,
) -> Admin | None:
    """
    Mount the SQLAdmin panel when admin credentials are configured.

    Purpose:
        Build the admin sub-application with password authentication and the
        three project model views, or leave it unmounted when disabled.

    Args:
        app: FastAPI application that receives the mounted admin.
        database: Shared database providing the async engine.
        settings: Application settings with admin credentials and flags.

    Returns:
        Admin | None: The configured admin instance, or None when the admin
            panel is disabled.

    Notes:
        - A missing or whitespace-only ``ADMIN_PASSWORD`` disables the panel.
        - ``AUTH_JWT_SECRET`` is required because it signs the session cookie.
        - No secret values are logged.
    """
    admin_password = settings.admin_password
    auth_jwt_secret = settings.auth_jwt_secret
    if not settings.admin_enabled or admin_password is None or auth_jwt_secret is None:
        logger.info(
            "admin.disabled",
            reason="ADMIN_PASSWORD or AUTH_JWT_SECRET is not set",
        )
        return None

    admin = Admin(
        app=app,
        engine=database.engine,
        base_url="/admin",
        title=f"{settings.app_name} Admin",
        authentication_backend=AdminAuth(
            username=settings.admin_username,
            password=admin_password,
            secret_key=auth_jwt_secret,
            session_max_age_seconds=settings.admin_session_max_age_seconds,
            https_only=settings.auth_refresh_cookie_secure,
        ),
        debug=settings.debug,
    )
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)
    logger.info("admin.mounted", base_url="/admin")
    return admin
