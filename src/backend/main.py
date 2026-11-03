from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from taskiq import AsyncBroker

from backend.admin.setup import setup_admin
from backend.api.errors.handlers import register_exception_handlers
from backend.api.errors.middleware import (
    AllowedHostsMiddleware,
    ErrorEnvelopeMiddleware,
)
from backend.api.router import api_router
from backend.api.routes import health
from backend.core.config import Settings, get_settings
from backend.core.logging import configure_logging
from backend.core.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from backend.db.session import Database


def custom_generate_unique_id(route: APIRoute) -> str:
    """
    Build a stable operation ID for a route.

    Purpose:
        Keep generated OpenAPI operation IDs deterministic across application
        restarts.

    Args:
        route: API route being added to the application.

    Returns:
        str: ``"<tag>-<name>"`` when the route has a tag, otherwise
            ``"default-<name>"``.

    Notes:
        - Routes without tags use the ``default`` prefix instead of failing.
    """
    tag = route.tags[0] if route.tags else "default"
    return f"{tag}-{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Manage application startup and shutdown resources.

    Purpose:
        Start the task broker and release it together with the shared database
        when the application stops.

    Args:
        app: Application whose state provides the database instance created
            during assembly.

    Yields:
        None: Control returns to the server while the application runs.

    Notes:
        - ``app.state.settings`` and ``app.state.db`` are set by ``create_app``
          before startup; the database is created during application assembly,
          not by the lifespan.
        - The broker singleton is created when ``backend.tasks.broker`` is
          imported; the API router imports example tasks at module import. The
          lifespan only starts and shuts it down.
        - Unit tests using the ASGI transport without a lifespan neither start
          the broker nor dispose the database.
        - The database is disposed even if broker shutdown fails.
    """
    database: Database = app.state.db
    broker = cast(
        AsyncBroker,
        importlib.import_module("backend.tasks.broker").broker,
    )

    await broker.startup()
    try:
        yield
    finally:
        try:
            await broker.shutdown()
        finally:
            await database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Purpose:
        Assemble the application factory with logging, middleware, exception
        handlers, health routes, the versioned API router, the shared database,
        and the optional SQLAdmin panel.

    Args:
        settings: Optional settings; environment-backed settings are used when
            omitted.

    Returns:
        FastAPI: Fully configured application instance.

    Raises:
        pydantic.ValidationError: If environment-backed settings are invalid.

    Notes:
        - ``app.state.settings`` is populated eagerly so exception handlers can
          read the debug flag without relying on the lifespan.
        - ``app.state.db`` is created during application assembly and disposed
          by the lifespan.
        - The admin panel is mounted only when both ``ADMIN_PASSWORD`` and
          ``AUTH_JWT_SECRET`` are configured; otherwise ``app.state.admin`` is
          ``None`` and ``/admin`` keeps the public JSON 404.
        - The broker singleton is created when ``backend.tasks.broker`` is
          imported, which happens while this module loads the API router; the
          lifespan only starts and shuts it down.
        - Middleware is added innermost first, so ``RequestContextMiddleware``
          remains the outermost layer with ``ErrorEnvelopeMiddleware`` directly
          inside it.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        generate_unique_id_function=custom_generate_unique_id,
        lifespan=lifespan,
    )
    app.state.settings = settings

    database = Database(settings)
    app.state.db = database

    if settings.allowed_hosts != ["*"]:
        app.add_middleware(AllowedHostsMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            max_requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    app.add_middleware(ErrorEnvelopeMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_str)
    app.state.admin = setup_admin(app, database, settings)
    return app


app = create_app()
