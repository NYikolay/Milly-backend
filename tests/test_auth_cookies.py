from __future__ import annotations

from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from backend.api.auth.cookies import (
    clear_refresh_cookie,
    refresh_cookie_path,
    set_refresh_cookie,
    validate_auth_origin,
)
from backend.core.config import Settings
from backend.core.errors import ForbiddenError


def _settings(**overrides: Any) -> Settings:
    """Build application settings with deterministic cookie and CORS values."""
    values: dict[str, Any] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)


def _request(*, origin: str | None = None) -> Request:
    """Build a minimal auth request with an optional Origin header."""
    headers = [] if origin is None else [(b"origin", origin.encode())]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/api/v1/auth/telegram",
            "query_string": b"",
            "headers": headers,
        }
    )


def test_cookie_path_follows_api_prefix() -> None:
    """Derive the refresh cookie path from the configured API prefix."""
    settings = _settings(api_v1_str="/api/v2")
    assert refresh_cookie_path(settings) == "/api/v2/auth"


def test_set_refresh_cookie_flags() -> None:
    """Set the refresh cookie with the hardened production flags."""
    settings = _settings()
    response = Response()
    set_refresh_cookie(response, "refresh-value", settings)
    header = response.headers["set-cookie"]
    assert "milly_refresh=refresh-value" in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header
    assert "Path=/api/v1/auth" in header
    assert "Max-Age=2592000" in header


def test_set_refresh_cookie_allows_insecure_local_mode() -> None:
    """Omit the Secure flag when local settings disable it."""
    settings = _settings(auth_refresh_cookie_secure=False)
    response = Response()
    set_refresh_cookie(response, "refresh-value", settings)
    assert "Secure" not in response.headers["set-cookie"]


def test_clear_refresh_cookie_uses_same_path() -> None:
    """Expire the refresh cookie on the same path used to set it."""
    settings = _settings()
    response = Response()
    clear_refresh_cookie(response, settings)
    header = response.headers["set-cookie"]
    assert "milly_refresh=" in header
    assert "Path=/api/v1/auth" in header
    assert "Max-Age=0" in header


def test_origin_guard_allows_missing_origin() -> None:
    """Allow non-browser clients that send no Origin header."""
    validate_auth_origin(_request(), _settings())


def test_origin_guard_allows_request_origin() -> None:
    """Allow requests whose Origin matches the request host."""
    validate_auth_origin(_request(origin="http://testserver"), _settings())


def test_origin_guard_allows_configured_cors_origin() -> None:
    """Allow an explicitly configured CORS origin."""
    settings = _settings(cors_origins=["https://app.example.com"])
    validate_auth_origin(_request(origin="https://app.example.com"), settings)


def test_origin_guard_allows_wildcard_cors() -> None:
    """Allow any explicit origin when CORS is configured with a wildcard."""
    settings = _settings(cors_origins=["*"])
    validate_auth_origin(_request(origin="https://any.example.com"), settings)


def test_origin_guard_rejects_unknown_origin() -> None:
    """Reject an explicit origin outside the same-origin and CORS allowlists."""
    settings = _settings(cors_origins=["https://app.example.com"])
    with pytest.raises(ForbiddenError) as exc_info:
        validate_auth_origin(_request(origin="https://evil.example.com"), settings)

    assert exc_info.value.code.value == "forbidden"
