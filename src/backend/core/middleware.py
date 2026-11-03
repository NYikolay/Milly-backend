from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.core.errors import PayloadTooLargeError, RateLimitedError

logger = structlog.get_logger(__name__)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_TRACKED_CLIENTS = 10_000
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


class BodySizeLimitMiddleware:
    """Reject HTTP requests whose body exceeds a configured byte limit."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        """
        Initialize the request body size limit middleware.

        Purpose:
            Store the wrapped ASGI application and the accepted body size so
            every request is checked against the same limit.

        Args:
            app: Next ASGI application in the middleware stack.
            max_body_bytes: Maximum accepted request body size in bytes.
        """
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Reject an HTTP request whose declared or streamed body exceeds the limit.

        Purpose:
            Stop oversized payloads before the application reads them, using
            Content-Length when available and counting streamed chunks
            otherwise.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Returns:
            None: This coroutine returns after the wrapped application
                completes.

        Raises:
            PayloadTooLargeError: If the declared Content-Length or the
                accumulated streamed body exceeds the configured limit.

        Notes:
            - Non-HTTP scopes pass through unchanged.
            - The body is never buffered or replayed; the wrapped receive is
              returned to the application unchanged unless the limit is
              exceeded.
            - An absent, malformed, or negative Content-Length is ignored and
              the streamed byte count is used instead.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > self.max_body_bytes:
                raise PayloadTooLargeError()

        received = 0

        async def receive_with_limit() -> Message:
            """
            Count incoming body bytes and reject once the limit is exceeded.

            Purpose:
                Enforce the body limit for streamed requests that lack a usable
                Content-Length while forwarding messages unchanged otherwise.

            Returns:
                Message: The next ASGI message from the wrapped receive.

            Raises:
                PayloadTooLargeError: If the accumulated body exceeds the
                    configured limit.
            """
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise PayloadTooLargeError()
            return message

        await self.app(scope, receive_with_limit, send)


class RateLimitMiddleware:
    """Reject HTTP requests that exceed a fixed-window per-client limit."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Initialize the per-client rate limit middleware.

        Purpose:
            Store the wrapped application, the fixed-window allowance, and the
            clock used to open new windows.

        Args:
            app: Next ASGI application in the middleware stack.
            max_requests: Maximum accepted requests per client and window.
            window_seconds: Length of the fixed window in seconds.
            clock: Monotonic clock returning seconds. Defaults to
                ``time.monotonic``.

        Raises:
            ValueError: If `max_requests` is less than 1 or `window_seconds` is
                not positive.

        Notes:
            - Counters live in process memory; separate workers do not share
              them.
            - The client address is taken from the ASGI ``client`` scope and
              never from forwarding headers.
        """
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.app = app
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Enforce the per-client request allowance for an HTTP request.

        Purpose:
            Count a request against its client's current fixed window and stop
            the request before routing when the allowance is exhausted.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.

        Returns:
            None: This coroutine returns after the wrapped application
                completes.

        Raises:
            RateLimitedError: If the client exceeds the allowance for the
                current window.

        Notes:
            - Non-HTTP scopes pass through unchanged.
            - The retry hint is at least one second and never shorter than the
              remaining window.
            - Expired windows are pruned once the tracked-client map exceeds a
              safety cap; the oldest tracked client is then evicted to bound
              memory.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        key = client[0] if client is not None else "unknown"
        now = self.clock()

        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= self.window_seconds:
            window_start = now
            count = 0
        count += 1
        self._windows[key] = (window_start, count)

        if len(self._windows) > _MAX_TRACKED_CLIENTS:
            self._prune(now)

        if count > self.max_requests:
            retry_after = max(1, math.ceil(window_start + self.window_seconds - now))
            raise RateLimitedError(headers={"Retry-After": str(retry_after)})

        await self.app(scope, receive, send)

    def _prune(self, now: float) -> None:
        """
        Drop expired client windows and bound the tracked-client map.

        Purpose:
            Keep memory usage bounded when many distinct client addresses are
            observed.

        Args:
            now: Current clock value in seconds.

        Returns:
            None: This method only mutates the tracked windows.

        Notes:
            - Expired entries are removed first.
            - If the map is still above the safety cap, entries are evicted in
              insertion order until the cap is met.
        """
        expired = [
            key
            for key, (window_start, _) in self._windows.items()
            if now - window_start >= self.window_seconds
        ]
        for key in expired:
            del self._windows[key]

        while len(self._windows) > _MAX_TRACKED_CLIENTS:
            oldest = next(iter(self._windows))
            del self._windows[oldest]
