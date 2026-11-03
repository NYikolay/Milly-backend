from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.core.config import Settings
from backend.main import create_app


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear ambient application environment variables for test isolation."""
    for name in (
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "ADMIN_SESSION_MAX_AGE_SECONDS",
        "AUTH_JWT_SECRET",
        "APP_ENV",
        "DEBUG",
        "AUTH_REFRESH_COOKIE_SECURE",
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings() -> Settings:
    """Build settings without reading a local .env file."""
    return Settings(_env_file=None, app_env="local", log_json=False)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """Create an application configured with the test settings."""
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield an HTTP client bound to the test application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
