from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from backend.core.config import Settings
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
