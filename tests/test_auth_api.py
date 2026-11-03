from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import CurrentAccessDep, get_auth_service, get_current_access
from backend.auth.service import LoginResult, TokenPair
from backend.auth.tokens import AccessTokenClaims
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.main import create_app
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
COOKIE_NAME = "milly_refresh"


def _login_result() -> LoginResult:
    """Build a login result backed by real ORM user and profile instances."""
    user = User(id=1, created_at=NOW, updated_at=NOW)
    profile = TelegramProfile(
        id=10,
        user_id=1,
        telegram_id=123,
        first_name="Ivan",
        is_premium=False,
    )
    profile.user = user
    return LoginResult(
        user=user,
        profile=profile,
        tokens=_token_pair(),
        is_new=False,
    )


def _token_pair() -> TokenPair:
    """Build a token pair with deterministic values and expiries."""
    return TokenPair(
        access_token="access-token",
        access_expires_at=NOW + timedelta(minutes=15),
        refresh_token="refresh-token",
        refresh_expires_at=NOW + timedelta(days=30),
    )


def _claims() -> AccessTokenClaims:
    """Build verified access claims for an authenticated user."""
    return AccessTokenClaims(
        user_id=1,
        session_id=77,
        telegram_id=123,
        jti="test-jti",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


class FakeAuthService:
    """Auth service double returning prepared values and recording calls."""

    def __init__(self, result: LoginResult) -> None:
        """Store the prepared login result and initialize call tracking."""
        self.result = result
        self.user: User | None = result.user
        self.login_calls = 0
        self.refresh_calls = 0
        self.logout_tokens: list[str | None] = []
        self.get_user_calls: list[int] = []
        self.login_error: Exception | None = None
        self.refresh_error: Exception | None = None

    async def login(self, raw_init_data: str) -> LoginResult:
        """Record a login call and return or raise the prepared outcome."""
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error
        return self.result

    async def refresh(self, raw_refresh_token: str) -> TokenPair:
        """Record a refresh call and return or raise the prepared outcome."""
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.result.tokens

    async def logout(self, raw_refresh_token: str | None) -> None:
        """Record the refresh token presented to logout."""
        self.logout_tokens.append(raw_refresh_token)

    async def get_user(self, user_id: int) -> User | None:
        """Record a user lookup and return the prepared user."""
        self.get_user_calls.append(user_id)
        return self.user


@pytest.fixture
def fake_service() -> FakeAuthService:
    """Build an auth service double with a prepared login result."""
    return FakeAuthService(_login_result())


@pytest.fixture
def auth_app(app: FastAPI, fake_service: FakeAuthService) -> FastAPI:
    """Bind the auth service override to the test application."""
    app.dependency_overrides[get_auth_service] = lambda: fake_service
    return app


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield an HTTP client using the auth service override."""
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_login_sets_refresh_cookie_flags(auth_client: AsyncClient) -> None:
    """Return an access token and set the hardened refresh cookie."""
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "valid"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header
    assert "Path=/api/v1/auth" in header


async def test_login_returns_user_profile(auth_client: AsyncClient) -> None:
    """Return the authenticated user with the Telegram profile."""
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "valid"},
    )
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["user"]["id"] == 1
    assert body["user"]["telegram_profile"]["telegram_id"] == 123


async def test_login_does_not_return_refresh_in_body(auth_client: AsyncClient) -> None:
    """Keep the refresh token out of the response body."""
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "valid"},
    )
    assert "refresh_token" not in response.json()
    assert "refresh-token" in response.headers["set-cookie"]


async def test_login_rejects_oversized_init_data(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Reject init data longer than 8192 characters before calling the service."""
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "x" * 8193},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["details"]["fields"][0]["field"] == "body.init_data"
    assert fake_service.login_calls == 0


async def test_login_accepts_init_data_at_max_length(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Accept init data of exactly 8192 characters."""
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "x" * 8192},
    )
    assert response.status_code == 200
    assert fake_service.login_calls == 1


async def test_login_invalid_init_data_returns_401(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Render a 401 unauthorized envelope when login validation fails."""
    fake_service.login_error = UnauthorizedError("Invalid init data")
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "invalid"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_login_forbidden_origin(fake_service: FakeAuthService) -> None:
    """Reject a login originated by an untrusted site."""
    settings = Settings(_env_file=None, cors_origins=["https://app.example.com"])
    app = create_app(settings)
    app.dependency_overrides[get_auth_service] = lambda: fake_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            json={"init_data": "valid"},
            headers={"Origin": "https://evil.example.com"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert fake_service.login_calls == 0


async def test_login_allowed_cors_origin(fake_service: FakeAuthService) -> None:
    """Accept a login from an explicitly allowed CORS origin."""
    settings = Settings(_env_file=None, cors_origins=["https://app.example.com"])
    app = create_app(settings)
    app.dependency_overrides[get_auth_service] = lambda: fake_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/telegram",
            json={"init_data": "valid"},
            headers={"Origin": "https://app.example.com"},
        )
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert fake_service.login_calls == 1


async def test_refresh_without_cookie_returns_401(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Reject refresh when the HttpOnly cookie is absent."""
    response = await auth_client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert fake_service.refresh_calls == 0


async def test_refresh_sets_new_cookie(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Rotate the refresh token and set the replacement cookie."""
    auth_client.cookies.set(COOKIE_NAME, "old-refresh-token")
    response = await auth_client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert response.json()["access_token"] == "access-token"
    assert fake_service.refresh_calls == 1
    assert "refresh-token" in response.headers["set-cookie"]


async def test_refresh_error_does_not_set_cookie(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Leave the existing cookie untouched when refresh fails."""
    fake_service.refresh_error = UnauthorizedError("Invalid refresh token")
    auth_client.cookies.set(COOKIE_NAME, "stale-refresh-token")
    response = await auth_client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


async def test_logout_clears_refresh_cookie(
    auth_client: AsyncClient,
    fake_service: FakeAuthService,
) -> None:
    """Revoke the session family and expire the refresh cookie."""
    auth_client.cookies.set(COOKIE_NAME, "refresh-token")
    response = await auth_client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert response.content == b""
    assert fake_service.logout_tokens == ["refresh-token"]
    assert "Max-Age=0" in response.headers["set-cookie"]


async def test_me_requires_bearer_token(auth_client: AsyncClient) -> None:
    """Reject profile requests without an Authorization header."""
    response = await auth_client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_returns_user(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    """Return the current user resolved from verified access claims."""
    auth_app.dependency_overrides[get_current_access] = lambda: _claims()
    response = await auth_client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["id"] == 1
    assert response.json()["telegram_profile"]["telegram_id"] == 123


async def test_protected_route_uses_access_claims_without_session(
    auth_app: FastAPI,
    auth_client: AsyncClient,
) -> None:
    """Resolve protected claims without opening a database session."""
    auth_app.dependency_overrides[get_current_access] = lambda: _claims()

    async def protected(claims: CurrentAccessDep) -> dict[str, int]:
        """Return the user identifier from the verified access claims."""
        return {"user_id": claims.user_id}

    auth_app.add_api_route("/_test/protected", protected, methods=["GET"])
    assert auth_app.state.db is not None
    response = await auth_client.get("/_test/protected")
    assert response.status_code == 200
    assert response.json() == {"user_id": 1}


async def test_auth_router_is_registered(auth_client: AsyncClient) -> None:
    """Expose the auth router under the versioned API prefix."""
    response = await auth_client.post(
        "/api/v1/auth/telegram",
        json={"init_data": "valid"},
    )
    assert response.status_code != 404
