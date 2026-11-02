from __future__ import annotations

from fastapi import Request, Response

from backend.core.config import Settings
from backend.core.errors import ForbiddenError


def refresh_cookie_path(settings: Settings) -> str:
    """
    Build the path scope for the refresh cookie.

    Purpose:
        Keep the HttpOnly refresh cookie limited to the auth routes and aligned
        with the configured API prefix.

    Args:
        settings: Application settings containing the versioned API prefix.

    Returns:
        str: Cookie path in the form ``"<api_v1_str>/auth"`` without a trailing
            slash on the prefix.
    """
    return f"{settings.api_v1_str.rstrip('/')}/auth"


def set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    """
    Attach a hardened refresh cookie to a response.

    Purpose:
        Store the opaque refresh token in an HttpOnly cookie scoped to the auth
        routes so browser JavaScript never reads it.

    Args:
        response: Response receiving the ``Set-Cookie`` header.
        token: Raw refresh token to store.
        settings: Application settings with the cookie name, security flags,
            same-site policy, TTL, and API prefix.

    Returns:
        None: The cookie header is added to ``response``.

    Notes:
        - ``Domain`` is never set, so the cookie stays host-only.
        - ``Max-Age`` uses ``auth_refresh_ttl_seconds``.
    """
    response.set_cookie(
        key=settings.auth_refresh_cookie_name,
        value=token,
        max_age=settings.auth_refresh_ttl_seconds,
        path=refresh_cookie_path(settings),
        secure=settings.auth_refresh_cookie_secure,
        httponly=True,
        samesite=settings.auth_refresh_cookie_samesite,
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    """
    Expire the refresh cookie on the same scope used to set it.

    Purpose:
        Remove the client refresh token after logout without leaving a stale
        cookie on a narrower or broader path.

    Args:
        response: Response receiving the ``Set-Cookie`` header.
        settings: Application settings with the cookie name, security flags,
            same-site policy, and API prefix.

    Returns:
        None: The expiry cookie header is added to ``response``.

    Notes:
        - The path, secure, HttpOnly, and same-site attributes mirror
          ``set_refresh_cookie`` so browsers replace the existing cookie.
    """
    response.delete_cookie(
        key=settings.auth_refresh_cookie_name,
        path=refresh_cookie_path(settings),
        secure=settings.auth_refresh_cookie_secure,
        httponly=True,
        samesite=settings.auth_refresh_cookie_samesite,
    )


def validate_auth_origin(request: Request, settings: Settings) -> None:
    """
    Enforce the same-site origin policy for cookie-authenticated auth routes.

    Purpose:
        Provide a lightweight CSRF guard for login, refresh, and logout by
        rejecting requests from origins that are neither the API origin nor an
        explicitly allowed CORS origin.

    Args:
        request: Incoming auth request whose ``Origin`` header is checked.
        settings: Application settings containing the allowed CORS origins.

    Returns:
        None: The request origin is allowed.

    Raises:
        ForbiddenError: If an explicit origin is neither the request origin nor
            an allowed CORS origin.

    Notes:
        - Requests without an ``Origin`` header are allowed, matching non-browser
          clients and same-origin form posts.
        - A wildcard CORS configuration allows every explicit origin.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin == request_origin:
        return
    if "*" in settings.cors_origins or origin in settings.cors_origins:
        return
    raise ForbiddenError("Origin not allowed")
