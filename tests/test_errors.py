from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from backend.core.config import Settings
from backend.main import create_app


class Payload(BaseModel):
    """Request payload used to trigger validation errors."""

    value: int


async def test_404_uses_error_envelope(client: AsyncClient) -> None:
    """Return the unified envelope for an unknown route."""
    response = await client.get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "http_404"
    assert body["error"]["request_id"]


async def test_validation_error_envelope(app: FastAPI, client: AsyncClient) -> None:
    """Return validation details in the unified envelope."""

    async def handler(payload: Payload) -> dict[str, int]:
        """Echo the validated payload value."""
        return {"value": payload.value}

    app.add_api_route("/_test/validate", handler, methods=["POST"])
    response = await client.post("/_test/validate", json={"value": "not-an-int"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"], list)


async def test_500_hides_details_in_production() -> None:
    """Hide unexpected error details when debug mode is off."""

    app = create_app(Settings(_env_file=None, app_env="production", debug=False, log_json=False))

    async def boom() -> None:
        """Raise an unexpected error."""
        raise RuntimeError("secret details")

    app.add_api_route("/_test/boom", boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret details" not in response.text


async def test_500_includes_security_and_cors_headers() -> None:
    """Keep security and CORS headers on responses from the outer error middleware."""

    app = create_app(
        Settings(
            _env_file=None,
            app_env="production",
            debug=False,
            log_json=False,
            cors_origins=["http://example.com"],
        )
    )

    async def boom() -> None:
        """Raise an unexpected error."""
        raise RuntimeError("boom")

    app.add_api_route("/_test/boom", boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get(
            "/_test/boom",
            headers={"Origin": "http://example.com", "X-Request-ID": "test-request-id"},
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["access-control-allow-origin"] == "http://example.com"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["access-control-expose-headers"] == "X-Request-ID"
    assert response.headers["Vary"] == "Origin"
    assert response.headers["X-Request-ID"] == "test-request-id"


async def test_docs_disabled_in_production() -> None:
    """Disable interactive docs when the environment is production."""
    app = create_app(Settings(_env_file=None, app_env="production", log_json=False))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/docs")
    assert response.status_code == 404
