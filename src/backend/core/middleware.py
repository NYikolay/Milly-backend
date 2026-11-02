from __future__ import annotations

import re
import time
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _resolve_request_id(scope: Scope) -> str:
    """
    Resolve the request ID for an HTTP request.

    Purpose:
        Reuse a caller-provided ``X-Request-ID`` only when it is safe and
        generate a fresh identifier otherwise.

    Args:
        scope: HTTP connection scope whose headers are inspected.

    Returns:
        str: The accepted incoming ID or a 32-character UUID4 hex string.

    Notes:
        - Accepted IDs match ``^[A-Za-z0-9._-]{1,64}$``.
    """
    candidate = Headers(scope=scope).get("x-request-id")
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


class RequestContextMiddleware:
    """Bind request ID and emit one structured access log per request."""

    def __init__(self, app: ASGIApp) -> None:
        """
        Initialize the request context middleware.

        Purpose:
            Store the wrapped ASGI application.

        Args:
            app: Next ASGI application in the middleware stack.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Process a request with logging context and access timing.

        Purpose:
            Resolve the request ID, bind request context variables, add the
            response header, and emit one completion event per HTTP request.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Returns:
            None: This coroutine returns after the wrapped application
                completes.

        Notes:
            - Non-HTTP scopes pass through unchanged.
            - Context variables are cleared after every request to avoid
              leakage between requests.
            - The request ID is also stored in the scope state so exception
              handlers running outside this middleware can include it in error
              envelopes.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        method = scope.get("method", "")
        path = scope.get("path", "")

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=method,
            path=path,
        )

        started = time.perf_counter()
        status_code: int | None = None

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception:
            if status_code is None:
                status_code = 500
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http.request.completed",
                request_id=request_id,
                status_code=status_code,
                duration_ms=duration_ms,
            )
            structlog.contextvars.clear_contextvars()


class SecurityHeadersMiddleware:
    """Add conservative security headers to HTTP responses."""

    def __init__(self, app: ASGIApp) -> None:
        """
        Initialize the security headers middleware.

        Purpose:
            Store the wrapped ASGI application.

        Args:
            app: Next ASGI application in the middleware stack.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Add security headers to responses produced by the wrapped application.

        Purpose:
            Set conservative browser security headers on responses produced by
            the wrapped application.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Returns:
            None: This coroutine returns after the wrapped application
                completes.

        Notes:
            - Non-HTTP scopes pass through unchanged.
            - Existing response headers with the same names are replaced.
            - Unhandled 500 responses bypass this middleware because they are
              produced by Starlette's outer ``ServerErrorMiddleware``; the
              unexpected-error handler adds the same headers from
              ``SECURITY_HEADERS``.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)
