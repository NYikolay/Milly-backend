from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.core.config import Settings
from backend.main import create_app

ADMIN_PASSWORD = "correct-horse-battery-staple"
JWT_SECRET = "a" * 32


def _admin_settings(**overrides: Any) -> Settings:
    """Build admin-enabled settings for tests."""
    values: dict[str, Any] = {
        "_env_file": None,
        "admin_username": "admin",
        "admin_password": ADMIN_PASSWORD,
        "auth_jwt_secret": JWT_SECRET,
        "log_json": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def admin_app() -> FastAPI:
    """Create an application with the admin panel enabled."""
    return create_app(_admin_settings(auth_refresh_cookie_secure=False))


@pytest.fixture
async def admin_client(admin_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield a client bound to the admin-enabled application."""
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_admin_is_not_mounted_without_password(client: AsyncClient) -> None:
    """Return the public JSON 404 when the admin is disabled."""
    response = await client.get("/admin/")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "not_found"
    assert body["request_id"] == response.headers["x-request-id"]


def test_admin_is_disabled_without_jwt_secret() -> None:
    """Do not mount the admin when the session secret is missing."""
    app = create_app(
        Settings(
            _env_file=None,
            admin_password=ADMIN_PASSWORD,
            log_json=False,
        )
    )
    assert app.state.admin is None


class RecordingLogger:
    """Minimal structlog-compatible logger recording calls."""

    def __init__(self) -> None:
        """Initialize the recorded call list."""
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        """Record an informational event."""
        self.calls.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        """Record a warning event."""
        self.calls.append(("warning", event, kwargs))


def test_admin_disabled_without_password_logs_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log an informational reason when the password is not configured."""
    recorder = RecordingLogger()
    monkeypatch.setattr("backend.admin.setup.logger", recorder)
    create_app(Settings(_env_file=None, auth_jwt_secret=JWT_SECRET, log_json=False))
    disabled = [call for call in recorder.calls if call[1] == "admin.disabled"]
    assert disabled[-1][0] == "info"
    assert "ADMIN_PASSWORD" in disabled[-1][2]["reason"]


def test_admin_disabled_without_jwt_secret_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warn when the password is configured but the session secret is missing."""
    recorder = RecordingLogger()
    monkeypatch.setattr("backend.admin.setup.logger", recorder)
    create_app(
        Settings(
            _env_file=None,
            admin_password=ADMIN_PASSWORD,
            log_json=False,
        )
    )
    disabled = [call for call in recorder.calls if call[1] == "admin.disabled"]
    assert disabled[-1][0] == "warning"
    assert "AUTH_JWT_SECRET" in disabled[-1][2]["reason"]


def test_create_app_exposes_database_before_lifespan(app: FastAPI) -> None:
    """Create the shared database instance during application assembly."""
    assert app.state.db is not None


def test_admin_registers_three_model_views(admin_app: FastAPI) -> None:
    """Expose the three model views on the mounted admin."""
    admin = admin_app.state.admin
    assert admin is not None
    assert [view.identity for view in admin.views] == [
        "user",
        "telegram-profile",
        "auth-session",
    ]


async def test_admin_login_and_logout(admin_client: AsyncClient) -> None:
    """Redirect to login, reject a wrong password, and accept a valid one."""
    response = await admin_client.get("/admin/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://test/admin/login"

    response = await admin_client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Invalid credentials" in response.text

    response = await admin_client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "http://test/admin/"
    assert "milly_admin_session" in admin_client.cookies

    response = await admin_client.get("/admin/")
    assert response.status_code == 200

    response = await admin_client.get("/admin/logout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://test/admin/"

    response = await admin_client.get("/admin/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://test/admin/login"


async def test_admin_session_cookie_attributes(admin_client: AsyncClient) -> None:
    """Scope the session cookie to /admin with the expected flags."""
    response = await admin_client.post(
        "/admin/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    set_cookie = response.headers["set-cookie"].lower()
    assert "milly_admin_session=" in set_cookie
    assert "path=/admin" in set_cookie
    assert "max-age=43200" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "; secure" not in set_cookie


async def test_admin_session_cookie_is_secure_in_production() -> None:
    """Enable the Secure cookie flag in production."""
    app = create_app(
        _admin_settings(
            app_env="production",
            telegram_bot_token="123:token",
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/admin/login",
            data={"username": "admin", "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
    assert "; secure" in response.headers["set-cookie"].lower()


async def test_password_change_invalidates_existing_session() -> None:
    """Reject an old cookie after ADMIN_PASSWORD changes."""
    first_app = create_app(_admin_settings(auth_refresh_cookie_secure=False))
    transport = ASGITransport(app=first_app)
    async with AsyncClient(transport=transport, base_url="http://test") as first_client:
        response = await first_client.post(
            "/admin/login",
            data={"username": "admin", "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 302
        cookie = first_client.cookies["milly_admin_session"]
        assert (await first_client.get("/admin/", follow_redirects=False)).status_code == 200

    second_app = create_app(
        _admin_settings(
            admin_password="another-strong-password",
            auth_refresh_cookie_secure=False,
        )
    )
    transport = ASGITransport(app=second_app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"milly_admin_session": cookie},
    ) as second_client:
        response = await second_client.get("/admin/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://test/admin/login"
