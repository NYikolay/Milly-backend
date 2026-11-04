from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from backend.core.config import Settings
from backend.main import create_app

PUBLIC_PATHS = {
    "/health/live",
    "/health/ready",
    "/api/v1/auth/telegram",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
}

_APP = create_app(
    Settings(
        _env_file=None,
        app_env="local",
        log_json=False,
        enable_docs=True,
        rate_limit_enabled=False,
        auth_jwt_secret=None,
        telegram_bot_token=None,
    )
)


def _iter_api_routes(
    routes: Sequence[Any],
    prefix: str = "",
) -> Iterator[tuple[str, APIRoute]]:
    """
    Walk application routes, including lazily included routers.

    Purpose:
        Enumerate the real operations exposed by the FastAPI 0.156 lazy router
        tree so the authorization matrix covers every endpoint.

    Args:
        routes: Route objects from an application or included router.
        prefix: Include prefix accumulated from parent routers.

    Yields:
        tuple[str, APIRoute]: Full path and the matching API route.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        original_router = getattr(route, "original_router", None)
        if original_router is None:
            continue
        include_context = getattr(route, "include_context", None)
        include_prefix = getattr(include_context, "prefix", "")
        yield from _iter_api_routes(original_router.routes, prefix + include_prefix)


def _protected_operations(app: FastAPI) -> Iterator[tuple[str, str]]:
    """
    Yield method/path pairs that must require a bearer token.

    Purpose:
        Build the authorization matrix from the application's actual routes
        while excluding public auth, health, and documentation endpoints.

    Args:
        app: Application whose API routes are inspected.

    Yields:
        tuple[str, str]: HTTP method and full route path.
    """
    for path, route in _iter_api_routes(app.routes):
        if path in PUBLIC_PATHS or path.startswith("/admin"):
            continue
        methods = route.methods or set()
        for method in methods - {"HEAD", "OPTIONS"}:
            yield method, path


def _materialize_path(path: str) -> str:
    """
    Replace path parameters with a concrete identifier.

    Purpose:
        Build a request URL that reaches the target route rather than failing
        parameter validation.

    Args:
        path: Route path that may contain ``{name}`` segments.

    Returns:
        str: Path with every parameter replaced by ``1``.
    """
    for parameter in re.findall(r"\{[^}]+\}", path):
        path = path.replace(parameter, "1")
    return path


_PROTECTED_OPERATIONS = sorted(_protected_operations(_APP), key=lambda item: (item[1], item[0]))


@pytest.fixture
async def bare_client() -> AsyncIterator[AsyncClient]:
    """Yield a client bound to an app without auth dependency overrides."""
    _APP.dependency_overrides.clear()
    transport = ASGITransport(app=_APP)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def test_protected_matrix_is_not_empty() -> None:
    """Ensure the authorization matrix discovers the protected API routes."""
    assert _PROTECTED_OPERATIONS
    assert ("GET", "/api/v1/tasks") in _PROTECTED_OPERATIONS
    assert ("POST", "/api/v1/tasks") in _PROTECTED_OPERATIONS
    assert ("GET", "/api/v1/auth/me") in _PROTECTED_OPERATIONS


@pytest.mark.parametrize(("method", "path"), _PROTECTED_OPERATIONS)
async def test_protected_route_requires_bearer(
    method: str,
    path: str,
    bare_client: AsyncClient,
) -> None:
    """Reject unauthenticated access to every protected operation."""
    response = await bare_client.request(method, _materialize_path(path))

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
