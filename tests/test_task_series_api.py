from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_current_access, get_series_service
from backend.auth.tokens import AccessTokenClaims
from backend.core.errors import NotFoundError
from backend.models.label import Label
from backend.models.task_reminder import TaskReminder
from backend.models.task_series import TaskSeries
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.series import SeriesDetail

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
GENERATED_THROUGH = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)


def _claims(user_id: int = 1) -> AccessTokenClaims:
    """Build verified access claims for an authenticated user."""
    return AccessTokenClaims(
        user_id=user_id,
        session_id=77,
        telegram_id=123,
        jti="test-jti",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def _series_detail(
    *,
    series_id: int = 1,
    user_id: int = 1,
    title: str = "Daily",
) -> SeriesDetail:
    """Build a series detail with deterministic labels and template reminders."""
    series = TaskSeries(
        id=series_id,
        user_id=user_id,
        project_id=None,
        title=title,
        description="Every day",
        priority=Priority.IMPORTANT_NOT_URGENT.value,
        frequency="daily",
        time_of_day=time(9, 0),
        timezone="Europe/Moscow",
        starts_on=date(2026, 11, 4),
        ends_on=None,
        generated_through=GENERATED_THROUGH,
        reminders_enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )
    label = Label(id=3, user_id=1, name="Home", color_id=1)
    reminder = TaskReminder(
        id=4,
        user_id=1,
        task_id=None,
        series_id=series_id,
        kind=ReminderKind.SCHEDULED.value,
        enabled=True,
        offset_minutes=None,
        interval_days=1,
        time_of_day=time(9, 0),
        timezone="Europe/Moscow",
        next_run_at=None,
    )
    return SeriesDetail(series=series, labels=[label], reminders=[reminder])


class FakeSeriesService:
    """Series service double returning prepared details and recording calls."""

    def __init__(self) -> None:
        """Initialize empty state, prepared results, and call tracking."""
        self.details: dict[int, SeriesDetail] = {}
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[int, dict[str, Any]]] = []
        self.deleted: list[int] = []
        self.user_calls: list[tuple[str, int]] = []
        self.get_error: Exception | None = None
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.delete_error: Exception | None = None

    def add_detail(self, detail: SeriesDetail) -> None:
        """Store a prepared series detail."""
        self.details[detail.series.id] = detail

    async def list_details(self, user_id: int) -> list[SeriesDetail]:
        """Return prepared series details owned by the user."""
        self.user_calls.append(("list", user_id))
        return [detail for detail in self.details.values() if detail.series.user_id == user_id]

    async def get_detail(self, user_id: int, series_id: int) -> SeriesDetail:
        """Return a prepared detail or raise the configured error."""
        self.user_calls.append(("get", user_id))
        if self.get_error is not None:
            raise self.get_error
        detail = self.details.get(series_id)
        if detail is None or detail.series.user_id != user_id:
            raise NotFoundError("Series not found")
        return detail

    async def create(self, user_id: int, **kwargs: Any) -> TaskSeries:
        """Record creation arguments and return the prepared series."""
        self.created.append({"user_id": user_id, **kwargs})
        if self.create_error is not None:
            raise self.create_error
        detail = self.details.get(1) or _series_detail()
        self.details[detail.series.id] = detail
        return detail.series

    async def update(self, user_id: int, series_id: int, **changes: Any) -> TaskSeries:
        """Record update arguments and apply simple field changes."""
        self.user_calls.append(("update", user_id))
        self.updated.append((series_id, dict(changes)))
        if self.update_error is not None:
            raise self.update_error
        detail = self.details.get(series_id)
        if detail is None or detail.series.user_id != user_id:
            raise NotFoundError("Series not found")
        for field, value in changes.items():
            if field in {"title", "description", "priority", "time_of_day", "timezone"}:
                setattr(detail.series, field, getattr(value, "value", value))
            elif field in {"ends_on", "project_id"}:
                setattr(detail.series, field, value)
        return detail.series

    async def delete(self, user_id: int, series_id: int) -> None:
        """Record deletion or raise the configured error."""
        self.user_calls.append(("delete", user_id))
        if self.delete_error is not None:
            raise self.delete_error
        detail = self.details.get(series_id)
        if detail is None or detail.series.user_id != user_id:
            raise NotFoundError("Series not found")
        self.deleted.append(series_id)
        self.details.pop(series_id, None)


@pytest.fixture
def fake_series_service() -> FakeSeriesService:
    """Build a series service double with no prepared details."""
    return FakeSeriesService()


@pytest.fixture
async def series_client(
    app: FastAPI,
    fake_series_service: FakeSeriesService,
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated against the fake series service."""
    app.dependency_overrides[get_series_service] = lambda: fake_series_service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_create_series_returns_schedule_and_generated_metadata(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Return the series schedule, generation horizon, labels, and reminders."""
    payload = {
        "title": "Daily",
        "description": "Every day",
        "priority": "important_not_urgent",
        "time_of_day": "09:00:00",
        "timezone": "Europe/Moscow",
        "starts_on": "2026-11-04",
        "reminders_enabled": True,
        "reminders": [
            {
                "kind": "scheduled",
                "interval_days": 1,
                "time_of_day": "09:00:00",
            }
        ],
    }

    response = await series_client.post("/api/v1/task-series", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["frequency"] == "daily"
    assert body["timezone"] == "Europe/Moscow"
    assert body["time_of_day"] == "09:00:00"
    assert body["starts_on"] == "2026-11-04"
    assert body["generated_through"] == GENERATED_THROUGH.isoformat().replace("+00:00", "Z")
    assert body["labels"] == [{"id": 3, "name": "Home"}]
    assert body["reminders"][0]["kind"] == "scheduled"
    recorded = fake_series_service.created[0]
    assert recorded["user_id"] == 1
    assert recorded["timezone"] == "Europe/Moscow"
    assert recorded["starts_on"] == date(2026, 11, 4)
    assert [item.kind for item in recorded["reminders"]] == [ReminderKind.SCHEDULED]


async def test_series_create_rejects_too_many_labels(series_client: AsyncClient) -> None:
    """Reject more than 100 label identifiers on series creation."""
    response = await series_client.post(
        "/api/v1/task-series",
        json={
            "title": "Many",
            "time_of_day": "09:00:00",
            "timezone": "UTC",
            "starts_on": "2026-11-04",
            "label_ids": list(range(1, 102)),
        },
    )

    assert response.status_code == 422


async def test_series_update_rejects_too_many_labels(series_client: AsyncClient) -> None:
    """Reject more than 100 label identifiers on series update."""
    response = await series_client.patch(
        "/api/v1/task-series/1",
        json={"label_ids": list(range(1, 102))},
    )

    assert response.status_code == 422


async def test_series_create_rejects_too_long_description(
    series_client: AsyncClient,
) -> None:
    """Reject a description longer than 5000 characters on series creation."""
    response = await series_client.post(
        "/api/v1/task-series",
        json={
            "title": "Long",
            "time_of_day": "09:00:00",
            "timezone": "UTC",
            "starts_on": "2026-11-04",
            "description": "x" * 5001,
        },
    )

    assert response.status_code == 422


async def test_series_update_rejects_too_long_description(
    series_client: AsyncClient,
) -> None:
    """Reject a description longer than 5000 characters on series update."""
    response = await series_client.patch(
        "/api/v1/task-series/1",
        json={"description": "x" * 5001},
    )

    assert response.status_code == 422


async def test_list_series_returns_details(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Return every series with its template metadata."""
    fake_series_service.add_detail(_series_detail())

    response = await series_client.get("/api/v1/task-series")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["title"] == "Daily"
    assert body["items"][0]["reminders"][0]["interval_days"] == 1


async def test_read_series_returns_detail(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Return one prepared series detail."""
    fake_series_service.add_detail(_series_detail())

    response = await series_client.get("/api/v1/task-series/1")

    assert response.status_code == 200
    assert response.json()["id"] == 1


async def test_update_series_calls_service(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Apply a partial update through the series service and return the result."""
    fake_series_service.add_detail(_series_detail())

    response = await series_client.patch(
        "/api/v1/task-series/1",
        json={"title": "Evening", "time_of_day": "18:00:00", "timezone": "UTC"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Evening"
    assert body["time_of_day"] == "18:00:00"
    assert body["timezone"] == "UTC"
    series_id, changes = fake_series_service.updated[0]
    assert series_id == 1
    assert changes == {
        "title": "Evening",
        "time_of_day": time(18, 0),
        "timezone": "UTC",
    }


async def test_delete_series_calls_service(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Delete a series and return an empty 204 response."""
    fake_series_service.add_detail(_series_detail())

    response = await series_client.delete("/api/v1/task-series/1")

    assert response.status_code == 204
    assert response.content == b""
    assert fake_series_service.deleted == [1]


async def test_another_users_series_returns_not_found(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Hide another user's series behind a 404 not-found envelope."""
    fake_series_service.get_error = NotFoundError("Series not found")

    response = await series_client.get("/api/v1/task-series/999")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["message"] == "Series not found"


async def test_create_series_rejects_invalid_timezone(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Reject an unknown IANA timezone with a 422 envelope."""
    response = await series_client.post(
        "/api/v1/task-series",
        json={
            "title": "Daily",
            "time_of_day": "09:00:00",
            "timezone": "Mars/Olympus",
            "starts_on": "2026-11-04",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert any(item["field"] == "body.timezone" for item in body["details"]["fields"])
    assert fake_series_service.created == []


async def test_update_series_rejects_invalid_timezone(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Reject an unknown IANA timezone on update with a 422 envelope."""
    response = await series_client.patch(
        "/api/v1/task-series/1",
        json={"timezone": "Not/AZone"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_series_service.updated == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/task-series", None),
        (
            "post",
            "/api/v1/task-series",
            {
                "title": "Daily",
                "time_of_day": "09:00:00",
                "timezone": "UTC",
                "starts_on": "2026-11-04",
            },
        ),
        ("get", "/api/v1/task-series/1", None),
        ("patch", "/api/v1/task-series/1", {"title": "Evening"}),
        ("delete", "/api/v1/task-series/1", None),
    ],
)
async def test_series_require_bearer_token(
    client: AsyncClient,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    """Require a bearer token on every series route."""
    response = await client.request(method, path, json=payload)

    assert response.status_code == 401
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_update_series_clears_nullable_fields_with_explicit_null(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Explicit nulls clear description, end date, and project on a series."""
    fake_series_service.add_detail(_series_detail())

    description = await series_client.patch(
        "/api/v1/task-series/1",
        json={"description": None},
    )
    ends_on = await series_client.patch("/api/v1/task-series/1", json={"ends_on": None})
    project = await series_client.patch("/api/v1/task-series/1", json={"project_id": None})

    assert description.status_code == 200
    assert description.json()["description"] is None
    assert ends_on.status_code == 200
    assert ends_on.json()["ends_on"] is None
    assert project.status_code == 200
    assert fake_series_service.updated == [
        (1, {"description": None}),
        (1, {"ends_on": None}),
        (1, {"project_id": None}),
    ]


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_series_routes_reject_aware_time_of_day(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
    method: str,
) -> None:
    """Reject a timezone-aware series wall-clock time with 422."""
    if method == "post":
        response = await series_client.post(
            "/api/v1/task-series",
            json={
                "title": "Daily",
                "time_of_day": "09:00:00+03:00",
                "timezone": "UTC",
                "starts_on": "2026-11-04",
            },
        )
    else:
        fake_series_service.add_detail(_series_detail())
        response = await series_client.patch(
            "/api/v1/task-series/1",
            json={"time_of_day": "09:00:00+03:00"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert fake_series_service.created == []
    assert fake_series_service.updated == []


async def test_create_series_rejects_out_of_range_interval(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Reject an interval above the SMALLINT database range."""
    response = await series_client.post(
        "/api/v1/task-series",
        json={
            "title": "Daily",
            "time_of_day": "09:00:00",
            "timezone": "UTC",
            "starts_on": "2026-11-04",
            "reminders": [
                {
                    "kind": "scheduled",
                    "interval_days": 32_768,
                    "time_of_day": "09:00:00",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_series_service.created == []


async def test_create_series_rejects_starts_on_outside_bounds(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """Reject a start date before the supported calendar-date lower bound."""
    response = await series_client.post(
        "/api/v1/task-series",
        json={
            "title": "Old",
            "time_of_day": "09:00:00",
            "timezone": "UTC",
            "starts_on": "2024-12-31",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert fake_series_service.created == []


async def test_update_another_users_series_returns_not_found(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """A cross-user series update is reported as not found."""
    fake_series_service.add_detail(_series_detail(user_id=2))

    response = await series_client.patch(
        "/api/v1/task-series/1",
        json={"title": "Changed"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_delete_another_users_series_returns_not_found(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """A cross-user series delete is reported as not found."""
    fake_series_service.add_detail(_series_detail(user_id=2))

    response = await series_client.delete("/api/v1/task-series/1")

    assert response.status_code == 404
    assert fake_series_service.deleted == []


async def test_series_routes_forward_the_claim_user_id(
    series_client: AsyncClient,
    fake_series_service: FakeSeriesService,
) -> None:
    """List, get, update, and delete pass the authenticated user identifier."""
    fake_series_service.add_detail(_series_detail())

    await series_client.get("/api/v1/task-series")
    await series_client.get("/api/v1/task-series/1")
    await series_client.patch("/api/v1/task-series/1", json={"title": "Changed"})
    await series_client.delete("/api/v1/task-series/1")

    assert ("list", 1) in fake_series_service.user_calls
    assert ("get", 1) in fake_series_service.user_calls
    assert ("update", 1) in fake_series_service.user_calls
    assert ("delete", 1) in fake_series_service.user_calls
