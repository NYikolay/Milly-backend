from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from backend.core.config import Settings
from backend.core.errors import PayloadTooLargeError, RateLimitedError
from backend.core.middleware import BodySizeLimitMiddleware, RateLimitMiddleware
from backend.main import create_app


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    """Echo a valid incoming request ID."""
    response = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


async def test_request_id_is_generated(client: AsyncClient) -> None:
    """Generate a request ID when the header is absent."""
    response = await client.get("/health/live")
    assert len(response.headers["X-Request-ID"]) == 32


async def test_invalid_request_id_is_replaced(client: AsyncClient) -> None:
    """Replace an incoming request ID that fails validation."""
    response = await client.get("/health/live", headers={"X-Request-ID": "bad id!"})
    assert response.headers["X-Request-ID"] != "bad id!"
    assert len(response.headers["X-Request-ID"]) == 32


async def test_security_headers(client: AsyncClient) -> None:
    """Add conservative security headers to HTTP responses."""
    response = await client.get("/health/live")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


async def test_body_size_limit_rejects_oversized_request() -> None:
    """Reject an oversized request with the flat error envelope before the route runs."""
    calls: list[str] = []
    app = create_app(
        Settings(
            _env_file=None,
            max_request_body_bytes=16,
            log_json=False,
        )
    )

    async def handler() -> dict[str, str]:
        """Record that the route ran."""
        calls.append("called")
        return {"status": "ok"}

    app.add_api_route("/_test/body", handler, methods=["POST"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/_test/body", content=b"x" * 32)
    assert response.status_code == 413
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "payload_too_large"
    assert body["request_id"]
    assert body["request_id"] == response.headers["x-request-id"]
    assert "details" not in body
    assert calls == []


async def test_body_size_limit_counts_streamed_body_without_content_length() -> None:
    """Count streamed chunks and raise once the accumulated body exceeds the limit."""
    received: list[bytes] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        """Read the wrapped request body until it completes."""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            received.append(message.get("body", b""))
            if not message.get("more_body", False):
                return

    middleware = BodySizeLimitMiddleware(inner, max_body_bytes=10)
    chunks: list[Message] = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": False},
    ]
    pending = iter(chunks)

    async def receive() -> Message:
        """Return the next scripted body chunk."""
        return next(pending)

    async def send(message: Message) -> None:
        """Discard outgoing messages."""

    scope: Scope = {"type": "http", "method": "POST", "path": "/", "headers": []}
    with pytest.raises(PayloadTooLargeError):
        await middleware(scope, receive, send)
    assert received == [b"12345", b"67890"]


async def test_rate_limit_rejects_excess_requests_with_envelope() -> None:
    """Reject requests above the per-IP limit with the flat error envelope."""
    app = create_app(
        Settings(
            _env_file=None,
            rate_limit_requests=2,
            rate_limit_window_seconds=60,
            log_json=False,
        )
    )

    async def handler() -> dict[str, str]:
        """Return a successful response."""
        return {"status": "ok"}

    app.add_api_route("/_test/limited", handler, methods=["GET"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/_test/limited")
        second = await client.get("/_test/limited")
        third = await client.get("/_test/limited")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    body = third.json()
    assert body["error"] is True
    assert body["code"] == "rate_limited"
    assert body["description"] == "Слишком много запросов"
    assert body["request_id"] == third.headers["x-request-id"]
    assert "details" not in body
    assert 1 <= int(third.headers["retry-after"]) <= 60


async def test_rate_limit_is_scoped_per_client_ip() -> None:
    """Track separate allowances for different client addresses."""
    app = create_app(
        Settings(
            _env_file=None,
            rate_limit_requests=1,
            rate_limit_window_seconds=60,
            log_json=False,
        )
    )

    async def handler() -> dict[str, str]:
        """Return a successful response."""
        return {"status": "ok"}

    app.add_api_route("/_test/limited", handler, methods=["GET"])
    first_transport = ASGITransport(app=app, client=("192.0.2.1", 1000))
    second_transport = ASGITransport(app=app, client=("192.0.2.2", 1000))
    async with (
        AsyncClient(transport=first_transport, base_url="http://test") as first_client,
        AsyncClient(transport=second_transport, base_url="http://test") as second_client,
    ):
        first_response = await first_client.get("/_test/limited")
        first_rejected = await first_client.get("/_test/limited")
        second_response = await second_client.get("/_test/limited")

    assert first_response.status_code == 200
    assert first_rejected.status_code == 429
    assert second_response.status_code == 200


async def test_rate_limit_window_resets_after_elapsed_time() -> None:
    """Allow requests again once the fixed window has elapsed."""
    now = [100.0]
    calls: list[str] = []

    def clock() -> float:
        """Return the current fake clock value."""
        return now[0]

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        """Record a forwarded request."""
        calls.append(scope["path"])

    middleware = RateLimitMiddleware(inner, max_requests=1, window_seconds=10, clock=clock)
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": ("203.0.113.1", 1234),
    }

    async def receive() -> Message:
        """Return an empty request message."""
        return {"type": "http.request"}

    async def send(message: Message) -> None:
        """Discard outgoing messages."""

    await middleware(scope, receive, send)
    with pytest.raises(RateLimitedError):
        await middleware(scope, receive, send)

    now[0] += 10
    await middleware(scope, receive, send)

    assert calls == ["/", "/"]


async def test_rate_limit_can_be_disabled() -> None:
    """Forward all requests when rate limiting is disabled."""
    app = create_app(
        Settings(
            _env_file=None,
            rate_limit_enabled=False,
            rate_limit_requests=1,
            rate_limit_window_seconds=60,
            log_json=False,
        )
    )

    async def handler() -> dict[str, str]:
        """Return a successful response."""
        return {"status": "ok"}

    app.add_api_route("/_test/unlimited", handler, methods=["GET"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/_test/unlimited")
        second = await client.get("/_test/unlimited")

    assert first.status_code == 200
    assert second.status_code == 200


async def test_cors_preflight() -> None:
    """Answer a CORS preflight for a configured origin."""
    app = create_app(
        Settings(
            _env_file=None,
            cors_origins=["http://example.com"],
            log_json=False,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/health/live",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://example.com"
