from __future__ import annotations

from urllib.parse import urlencode

from pydantic import SecretStr
from starlette.requests import Request
from starlette.types import Message, Scope

from backend.admin.auth import AdminAuth


def _request(
    *,
    form: dict[str, str] | None = None,
    session: dict[str, str] | None = None,
) -> Request:
    """Build a request with an optional URL-encoded form and session."""
    body = urlencode(form or {}).encode("utf-8")
    messages: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> Message:
        """Return the next scripted request message."""
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "method": "POST" if form is not None else "GET",
        "path": "/admin/login",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "session": {} if session is None else dict(session),
    }
    return Request(scope, receive)


def _auth(password: str = "correct-horse-battery") -> AdminAuth:
    """Build an authentication backend with deterministic test settings."""
    return AdminAuth(
        username="admin",
        password=SecretStr(password),
        secret_key=SecretStr("a" * 32),
        session_max_age_seconds=43_200,
        https_only=False,
    )


def test_session_cookie_is_scoped_to_admin() -> None:
    """Configure the session cookie name, path, lifetime, and flags."""
    middleware = _auth().middlewares[0]
    assert middleware.kwargs["session_cookie"] == "milly_admin_session"
    assert middleware.kwargs["path"] == "/admin"
    assert middleware.kwargs["max_age"] == 43_200
    assert middleware.kwargs["same_site"] == "lax"
    assert middleware.kwargs["https_only"] is False


async def test_login_accepts_valid_credentials() -> None:
    """Accept valid credentials and authenticate the same session."""
    auth = _auth()
    request = _request(form={"username": "admin", "password": "correct-horse-battery"})
    assert await auth.login(request) is True
    assert await auth.authenticate(request) is True


async def test_login_rejects_wrong_username_or_password() -> None:
    """Reject a wrong username and a wrong password."""
    auth = _auth()
    assert (
        await auth.login(_request(form={"username": "other", "password": "correct-horse-battery"}))
        is False
    )
    assert await auth.login(_request(form={"username": "admin", "password": "wrong"})) is False


async def test_authenticate_rejects_missing_or_forged_session() -> None:
    """Reject requests without a session or with a forged fingerprint."""
    auth = _auth()
    assert await auth.authenticate(_request()) is False
    assert await auth.authenticate(_request(session={"authenticated": "forged"})) is False


async def test_password_change_invalidates_existing_session() -> None:
    """Reject a session created with a different admin password."""
    old_auth = _auth("old-password")
    new_auth = _auth("new-password")
    request = _request(form={"username": "admin", "password": "old-password"})
    assert await old_auth.login(request) is True
    assert await new_auth.authenticate(request) is False


async def test_logout_clears_session() -> None:
    """Clear the authenticated session on logout."""
    auth = _auth()
    request = _request(form={"username": "admin", "password": "correct-horse-battery"})
    assert await auth.login(request) is True
    assert await auth.logout(request) is True
    assert await auth.authenticate(request) is False


async def test_non_ascii_password_is_supported() -> None:
    """Compare non-ASCII passwords as UTF-8 bytes."""
    auth = _auth("пароль-владельца")
    request = _request(form={"username": "admin", "password": "пароль-владельца"})
    assert await auth.login(request) is True
