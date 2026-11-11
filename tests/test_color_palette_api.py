from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_current_access, get_palette_service
from backend.domain.auth.models import AccessTokenClaims
from backend.domain.entities import ColorPalette

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _claims() -> AccessTokenClaims:
    """Build verified access claims for an authenticated user."""
    return AccessTokenClaims(
        user_id=1,
        session_id=77,
        telegram_id=123,
        jti="test-jti",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


class FakePaletteService:
    """Palette service double returning prepared colors."""

    def __init__(self, colors: list[ColorPalette]) -> None:
        """
        Store prepared colors.

        Purpose:
            Provide deterministic palette data to the API route.

        Args:
            colors: Colors returned by the service, including inactive rows.
        """
        self.colors = colors
        self.list_calls = 0

    async def list_active(self) -> list[ColorPalette]:
        """Return active colors sorted by display position."""
        self.list_calls += 1
        return sorted(
            (color for color in self.colors if color.is_active),
            key=lambda color: (color.sort_order, color.id),
        )


@pytest.fixture
def fake_palette_service() -> FakePaletteService:
    """Build a palette service double with active and inactive colors."""
    return FakePaletteService(
        [
            ColorPalette(
                id=3,
                slug="green",
                name="Green",
                hex="#00AA00",
                sort_order=2,
                is_active=True,
            ),
            ColorPalette(
                id=1,
                slug="blue",
                name="Blue",
                hex="#0000FF",
                sort_order=1,
                is_active=True,
            ),
            ColorPalette(
                id=2,
                slug="retired",
                name="Retired",
                hex="#999999",
                sort_order=0,
                is_active=False,
            ),
        ]
    )


@pytest.fixture
async def palette_client(
    app: FastAPI,
    fake_palette_service: FakePaletteService,
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated against the fake palette service."""
    app.dependency_overrides[get_palette_service] = lambda: fake_palette_service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_palette_returns_active_colors_in_display_order(
    palette_client: AsyncClient,
    fake_palette_service: FakePaletteService,
) -> None:
    """Return only active colors ordered by sort order and identifier."""
    response = await palette_client.get("/api/v1/color-palette")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"id": 1, "slug": "blue", "name": "Blue", "hex": "#0000FF"},
            {"id": 3, "slug": "green", "name": "Green", "hex": "#00AA00"},
        ]
    }
    assert fake_palette_service.list_calls == 1


async def test_palette_requires_bearer_token(client: AsyncClient) -> None:
    """Reject an unauthenticated palette request with the unified envelope."""
    response = await client.get("/api/v1/color-palette")

    assert response.status_code == 401
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "unauthorized"
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["www-authenticate"] == "Bearer"
