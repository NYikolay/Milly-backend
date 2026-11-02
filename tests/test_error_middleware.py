from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from taskiq.exceptions import SendTaskError

from backend.api.errors.middleware import (
    AllowedHostsMiddleware,
    ErrorEnvelopeMiddleware,
)
from backend.core.config import Settings
from backend.core.errors import BadRequestError
from backend.core.logging import configure_logging
from backend.core.middleware import RequestContextMiddleware
from backend.main import create_app


class RaisingMiddleware:
    """Raise a configured error before the wrapped application runs."""

    def __init__(self, app: ASGIApp, exc: Exception | None = None) -> None:
        """Store the wrapped ASGI application and the error to raise."""
        self.app = app
        self.exc = exc if exc is not None else BadRequestError(message="middleware failure")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Raise the configured error for every request."""
        raise self.exc


async def _ok(request: Request) -> PlainTextResponse:
    """Return a successful plain-text response."""
    return PlainTextResponse("ok")


def _middleware_app(settings: Settings, exc: Exception | None = None) -> Starlette:
    """Build an application whose inner middleware always raises."""
    return Starlette(
        routes=[Route("/", _ok)],
        middleware=[
            Middleware(RequestContextMiddleware),
            Middleware(ErrorEnvelopeMiddleware, settings=settings),
            Middleware(RaisingMiddleware, exc=exc),
        ],
    )


async def test_middleware_error_uses_envelope(settings: Settings) -> None:
    """Render a middleware failure as the flat error envelope."""
    transport = ASGITransport(app=_middleware_app(settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["request_id"] == response.headers["x-request-id"]
    assert len(response.headers["x-request-id"]) == 32


@pytest.mark.parametrize(
    ("exc", "status_code", "code"),
    [
        pytest.param(
            StarletteHTTPException(401, detail="Token expired"),
            401,
            "unauthorized",
            id="http-401",
        ),
        pytest.param(
            OperationalError("SELECT 1", {}, Exception("down")),
            503,
            "database_unavailable",
            id="database-operational",
        ),
        pytest.param(SendTaskError(), 503, "task_enqueue_failed", id="taskiq-send"),
    ],
)
async def test_middleware_error_branches_use_envelope(
    settings: Settings,
    exc: Exception,
    status_code: int,
    code: str,
) -> None:
    """Render mapped middleware failures with the envelope and a matching request ID."""
    transport = ASGITransport(app=_middleware_app(settings, exc))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == status_code
    body = response.json()
    assert body["error"] is True
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert len(response.headers["x-request-id"]) == 32


async def test_disallowed_cors_preflight_stays_plain_text() -> None:
    """Keep Starlette's plain-text rejection as the documented envelope exception."""
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
                "Origin": "http://other.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 400
    assert response.text == "Disallowed CORS origin"
    assert response.headers["content-type"].startswith("text/plain")


def test_error_envelope_is_inside_request_context(settings: Settings) -> None:
    """Install the error envelope directly inside the request context."""
    middleware_classes: list[object] = [
        middleware.cls for middleware in create_app(settings).user_middleware
    ]
    request_context_index = middleware_classes.index(RequestContextMiddleware)
    assert middleware_classes.index(ErrorEnvelopeMiddleware) == request_context_index + 1


async def test_access_log_uses_rendered_status(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Log the status of the error envelope with a temporarily attached capture handler."""
    configure_logging(settings)
    root_logger = logging.getLogger()
    root_logger.addHandler(caplog.handler)
    transport = ASGITransport(app=_middleware_app(settings))
    try:
        with caplog.at_level(logging.INFO):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/")
    finally:
        root_logger.removeHandler(caplog.handler)
    assert response.status_code == 400
    completed_events = [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("event") == "http.request.completed"
    ]
    assert completed_events
    assert completed_events[-1]["status_code"] == 400


async def test_disallowed_host_uses_error_envelope() -> None:
    """Render a disallowed Host header as the flat error envelope."""
    app = create_app(
        Settings(
            _env_file=None,
            allowed_hosts=["api.example.com"],
            log_json=False,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://other.example.com") as client:
        response = await client.get("/health/live")
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_www_redirect_preserved() -> None:
    """Redirect a bare host to the configured www host."""
    app = create_app(
        Settings(
            _env_file=None,
            allowed_hosts=["www.example.com"],
            log_json=False,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://example.com") as client:
        response = await client.get("/health/live")
    assert response.status_code == 307
    assert response.headers["location"] == "http://www.example.com/health/live"


async def test_allowed_hosts_passes_non_http_scope() -> None:
    """Pass non-HTTP scopes to the wrapped application without validation."""
    calls: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        """Record the scope passed by the middleware."""
        calls.append(scope)

    async def receive() -> Message:
        """Return a websocket disconnect message."""
        return {"type": "websocket.disconnect"}

    async def send(message: Message) -> None:
        """Discard outgoing messages."""
        return None

    middleware = AllowedHostsMiddleware(inner, allowed_hosts=["api.example.com"])
    await middleware({"type": "websocket"}, receive, send)
    assert len(calls) == 1
