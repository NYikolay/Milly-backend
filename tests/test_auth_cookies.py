from starlette.responses import Response

from backend.auth.cookies import (
    clear_refresh_cookie,
    refresh_cookie_path,
    set_refresh_cookie,
)
from backend.core.config import Settings


def test_cookie_path_follows_api_prefix() -> None:
    """Derive the refresh cookie path from the configured API prefix."""
    settings = Settings(_env_file=None, api_v1_str="/api/v2")
    assert refresh_cookie_path(settings) == "/api/v2/auth"


def test_set_refresh_cookie_flags() -> None:
    """Set the refresh cookie with the hardened production flags."""
    settings = Settings(_env_file=None)
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
    settings = Settings(_env_file=None, auth_refresh_cookie_secure=False)
    response = Response()
    set_refresh_cookie(response, "refresh-value", settings)
    assert "Secure" not in response.headers["set-cookie"]


def test_clear_refresh_cookie_uses_same_path() -> None:
    """Expire the refresh cookie on the same path used to set it."""
    settings = Settings(_env_file=None)
    response = Response()
    clear_refresh_cookie(response, settings)
    header = response.headers["set-cookie"]
    assert "milly_refresh=" in header
    assert "Path=/api/v1/auth" in header
    assert "Max-Age=0" in header
