from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_database_probe
from backend.core.config import Settings
from backend.main import create_app


class FakeProbe:
    """Readiness probe double returning a fixed result."""

    def __init__(self, ok: bool) -> None:
        """Store the readiness result returned by the fake probe."""
        self.ok = ok

    async def check(self) -> bool:
        """Return the configured readiness result."""
        return self.ok


async def test_live(client: AsyncClient) -> None:
    """Report liveness without checking dependencies."""
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_ok(app: FastAPI, client: AsyncClient) -> None:
    """Report readiness when the database probe succeeds."""
    app.dependency_overrides[get_database_probe] = lambda: FakeProbe(True)
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_ready_down(app: FastAPI, client: AsyncClient) -> None:
    """Report unavailability through the unified error envelope when the probe fails."""
    app.dependency_overrides[get_database_probe] = lambda: FakeProbe(False)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "database_unavailable"
    assert body["details"] == {"database": "down"}
    assert "status" not in body


async def test_live_with_configured_allowed_host() -> None:
    """Serve liveness when the request Host matches the configured allowlist."""
    app = create_app(
        Settings(
            _env_file=None,
            allowed_hosts=["api.example.com"],
            log_json=False,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://api.example.com") as http_client:
        response = await http_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_live_with_disallowed_host() -> None:
    """Reject a request whose Host is not in the configured allowlist."""
    app = create_app(
        Settings(
            _env_file=None,
            allowed_hosts=["api.example.com"],
            log_json=False,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://other.example.com") as http_client:
        response = await http_client.get("/health/live")
    assert response.status_code == 400
