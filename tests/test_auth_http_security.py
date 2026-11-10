from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_auth_service
from backend.core.config import Settings
from backend.infrastructure.auth.tokens import create_access_token
from backend.main import create_app

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
SECRET = "test-secret-" + "a" * 32
OTHER_SECRET = "test-secret-" + "b" * 32


def _settings(**overrides: Any) -> Settings:
    """Build local auth settings with a deterministic JWT secret."""
    values: dict[str, Any] = {
        "_env_file": None,
        "app_env": "local",
        "log_json": False,
        "auth_jwt_secret": SECRET,
        "cors_origins": [],
        "rate_limit_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class StubAuthService:
    """Auth service double that fails if a security test reaches storage."""

    async def login(self, raw_init_data: str) -> None:
        """Fail when login unexpectedly reaches the service."""
        raise AssertionError("login must not be called")

    async def refresh(self, raw_refresh_token: str) -> None:
        """Fail when refresh unexpectedly reaches the service."""
        raise AssertionError("refresh must not be called")

    async def logout(self, raw_refresh_token: str | None) -> None:
        """Fail when logout unexpectedly reaches the service."""
        raise AssertionError("logout must not be called")

    async def get_user(self, user_id: int) -> None:
        """Fail when the current-user lookup unexpectedly reaches the service."""
        raise AssertionError("get_user must not be called")

    async def update_timezone(self, user_id: int, timezone: str) -> None:
        """Fail when a timezone update unexpectedly reaches the service."""
        raise AssertionError("update_timezone must not be called")


@pytest.fixture
def security_app() -> FastAPI:
    """Build an app with JWT security settings and a storage-free auth service."""
    app = create_app(_settings())
    app.dependency_overrides[get_auth_service] = lambda: StubAuthService()
    return app


@pytest.fixture
async def security_client(security_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield a client bound to the security-focused application."""
    transport = ASGITransport(app=security_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_me_rejects_malformed_bearer(security_client: AsyncClient) -> None:
    """Reject a bearer value that is not a JWT."""
    response = await security_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_me_rejects_expired_token(security_client: AsyncClient) -> None:
    """Reject an access token issued beyond the configured TTL."""
    token, _ = create_access_token(
        1,
        2,
        3,
        settings=_settings(),
        now=NOW - timedelta(days=1),
    )

    response = await security_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_tasks_rejects_wrong_secret_token(security_client: AsyncClient) -> None:
    """Reject a token signed with a different JWT secret."""
    token, _ = create_access_token(
        1,
        2,
        3,
        settings=_settings(auth_jwt_secret=OTHER_SECRET),
        now=datetime.now(UTC),
    )

    response = await security_client.get(
        "/api/v1/tasks",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_refresh_rejects_disallowed_origin(security_client: AsyncClient) -> None:
    """Reject refresh requests from an origin outside the CORS allowlist."""
    response = await security_client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_logout_rejects_disallowed_origin(security_client: AsyncClient) -> None:
    """Reject logout requests from an origin outside the CORS allowlist."""
    response = await security_client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_refresh_without_origin_reaches_cookie_check(
    security_client: AsyncClient,
) -> None:
    """Treat an origin-less refresh as a missing cookie rather than forbidden."""
    response = await security_client.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
