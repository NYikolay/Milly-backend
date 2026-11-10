from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_current_access, get_label_service, get_palette_service
from backend.core.errors import ConflictError, NotFoundError
from backend.domain.auth.models import AccessTokenClaims
from backend.domain.entities import ColorPalette, Label

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


def _color(color_id: int = 1) -> ColorPalette:
    """Build a palette color used in prepared label data."""
    return ColorPalette(
        id=color_id,
        slug="blue",
        name="Blue",
        hex="#0000FF",
        sort_order=1,
        is_active=True,
    )


class FakePaletteService:
    """Palette service double resolving prepared colors by identifier."""

    def __init__(self, colors: list[ColorPalette]) -> None:
        """Store prepared colors and initialize lookup tracking."""
        self.colors = {color.id: color for color in colors}
        self.get_calls: list[int] = []

    async def get_color(self, color_id: int) -> ColorPalette:
        """Record a lookup and return the matching prepared color."""
        self.get_calls.append(color_id)
        color = self.colors.get(color_id)
        if color is None:
            raise NotFoundError("Palette color not found")
        return color


class FakeLabelService:
    """Label service double backed by in-memory labels."""

    def __init__(self) -> None:
        """Initialize empty label state and prepared errors."""
        self.labels: dict[int, Label] = {}
        self.colors: dict[int, ColorPalette] = {1: _color()}
        self.created: list[dict[str, object]] = []
        self.updated: list[tuple[int, dict[str, object]]] = []
        self.deleted: list[int] = []
        self.user_calls: list[tuple[str, int]] = []
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.delete_error: Exception | None = None
        self._next_id = 2

    def add_label(self, label: Label) -> None:
        """Store a prepared label."""
        self.labels[label.id] = label

    async def list(self, user_id: int) -> list[Label]:
        """Return every prepared label owned by the user."""
        self.user_calls.append(("list", user_id))
        return [label for label in self.labels.values() if label.user_id == user_id]

    async def create(
        self,
        user_id: int,
        *,
        name: str,
        color_id: int | None = None,
    ) -> Label:
        """Record creation arguments and store a new label."""
        self.created.append(
            {
                "user_id": user_id,
                "name": name,
                "color_id": color_id,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        color = self.colors.get(color_id, _color()) if color_id is not None else _color()
        label = Label(
            id=self._next_id,
            user_id=user_id,
            name=name,
            color_id=color.id,
            created_at=NOW,
            updated_at=NOW,
        )
        self._next_id += 1
        self.labels[label.id] = label
        return label

    async def update(
        self,
        user_id: int,
        label_id: int,
        **changes: object,
    ) -> Label:
        """Record update arguments and apply the prepared changes."""
        self.user_calls.append(("update", user_id))
        self.updated.append((label_id, dict(changes)))
        if self.update_error is not None:
            raise self.update_error
        label = self.labels.get(label_id)
        if label is None or label.user_id != user_id:
            raise NotFoundError("Label not found")
        name = changes.get("name")
        if isinstance(name, str):
            label.name = name
        color_id = changes.get("color_id")
        if isinstance(color_id, int):
            label.color_id = color_id
        return label

    async def delete(self, user_id: int, label_id: int) -> None:
        """Record deletion or raise the configured error."""
        self.user_calls.append(("delete", user_id))
        if self.delete_error is not None:
            raise self.delete_error
        label = self.labels.get(label_id)
        if label is None or label.user_id != user_id:
            return
        self.deleted.append(label_id)
        self.labels.pop(label_id, None)


@pytest.fixture
def fake_palette_service() -> FakePaletteService:
    """Build a palette service double with a default color."""
    return FakePaletteService([_color()])


@pytest.fixture
def fake_label_service() -> FakeLabelService:
    """Build a label service double with no prepared labels."""
    return FakeLabelService()


@pytest.fixture
async def label_client(
    app: FastAPI,
    fake_label_service: FakeLabelService,
    fake_palette_service: FakePaletteService,
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated against the fake label services."""
    app.dependency_overrides[get_label_service] = lambda: fake_label_service
    app.dependency_overrides[get_palette_service] = lambda: fake_palette_service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_list_labels_returns_color_data(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """Return owned labels with their resolved palette color."""
    fake_label_service.add_label(
        Label(
            id=1,
            user_id=1,
            name="Urgent",
            color_id=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    response = await label_client.get("/api/v1/labels")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": 1,
                "name": "Urgent",
                "color": {
                    "id": 1,
                    "slug": "blue",
                    "name": "Blue",
                    "hex": "#0000FF",
                },
                "created_at": "2026-11-04T12:00:00Z",
                "updated_at": "2026-11-04T12:00:00Z",
            }
        ]
    }


async def test_create_label_defaults_color(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """Trim the name, delegate default color selection, and return 201."""
    response = await label_client.post("/api/v1/labels", json={"name": "  Urgent  "})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Urgent"
    assert body["color"]["id"] == 1
    assert fake_label_service.created == [{"user_id": 1, "name": "Urgent", "color_id": None}]


async def test_create_label_duplicate_name_returns_conflict(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """Render a duplicate label name as a 409 conflict envelope."""
    fake_label_service.create_error = ConflictError("Label name already exists")

    response = await label_client.post("/api/v1/labels", json={"name": "Urgent"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "conflict"
    assert body["message"] == "Label name already exists"
    assert body["description"] == "Операция конфликтует с текущим состоянием"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "details" not in body


async def test_update_label_returns_updated_color(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
    fake_palette_service: FakePaletteService,
) -> None:
    """Apply a partial update and return the refreshed label."""
    fake_palette_service.colors[2] = ColorPalette(
        id=2,
        slug="green",
        name="Green",
        hex="#00AA00",
        sort_order=2,
        is_active=True,
    )
    fake_label_service.add_label(
        Label(
            id=1,
            user_id=1,
            name="Urgent",
            color_id=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    response = await label_client.patch(
        "/api/v1/labels/1",
        json={"name": "Later", "color_id": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Later"
    assert body["color"]["id"] == 2
    assert fake_label_service.updated == [(1, {"name": "Later", "color_id": 2})]
    assert fake_palette_service.get_calls == [2]


async def test_update_label_duplicate_name_returns_conflict(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """Render a duplicate name during update as a 409 conflict envelope."""
    fake_label_service.add_label(
        Label(
            id=1,
            user_id=1,
            name="Urgent",
            color_id=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    fake_label_service.update_error = ConflictError("Label name already exists")

    response = await label_client.patch(
        "/api/v1/labels/1",
        json={"name": "Later"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


async def test_delete_label_returns_no_content(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """Delete a label and return an empty 204 response."""
    fake_label_service.add_label(
        Label(
            id=1,
            user_id=1,
            name="Urgent",
            color_id=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    response = await label_client.delete("/api/v1/labels/1")

    assert response.status_code == 204
    assert response.content == b""
    assert fake_label_service.deleted == [1]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"name": "   "}, "body.name"),
        ({"name": "Urgent", "color_id": 0}, "body.color_id"),
    ],
)
async def test_create_label_validates_input(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
    payload: dict[str, object],
    field: str,
) -> None:
    """Reject blank names and non-positive color identifiers with 422."""
    response = await label_client.post("/api/v1/labels", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert any(item["field"] == field for item in body["details"]["fields"])
    assert fake_label_service.created == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/labels", None),
        ("post", "/api/v1/labels", {"name": "Urgent"}),
        ("patch", "/api/v1/labels/1", {"name": "Later"}),
        ("delete", "/api/v1/labels/1", None),
    ],
)
async def test_labels_require_bearer_token(
    client: AsyncClient,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    """Require a bearer token on every label route."""
    response = await client.request(method, path, json=payload)

    assert response.status_code == 401
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "unauthorized"
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["www-authenticate"] == "Bearer"


async def test_update_another_users_label_returns_not_found(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """A cross-user label update is reported as not found."""
    fake_label_service.add_label(
        Label(id=1, user_id=2, name="Private", color_id=1, created_at=NOW, updated_at=NOW)
    )

    response = await label_client.patch("/api/v1/labels/1", json={"name": "Changed"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_label_routes_forward_the_claim_user_id(
    label_client: AsyncClient,
    fake_label_service: FakeLabelService,
) -> None:
    """List, update, and delete pass the authenticated user identifier."""
    fake_label_service.add_label(
        Label(id=1, user_id=1, name="Urgent", color_id=1, created_at=NOW, updated_at=NOW)
    )

    await label_client.get("/api/v1/labels")
    await label_client.patch("/api/v1/labels/1", json={"name": "Later"})
    await label_client.delete("/api/v1/labels/1")

    assert ("list", 1) in fake_label_service.user_calls
    assert ("update", 1) in fake_label_service.user_calls
    assert ("delete", 1) in fake_label_service.user_calls


def test_label_route_does_not_import_orm() -> None:
    """Label routes use mappers instead of ORM validation."""
    source = Path("src/backend/api/routes/labels.py").read_text(encoding="utf-8")

    assert "backend.models" not in source
    assert "model_validate" not in source
    assert "def _label_response" not in source
