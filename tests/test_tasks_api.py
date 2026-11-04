from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_current_access, get_task_service
from backend.auth.tokens import AccessTokenClaims
from backend.core.errors import NotFoundError
from backend.models.label import Label
from backend.models.task import Task
from backend.models.task_reminder import TaskReminder
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.tasks import TaskDetail, TaskFilters

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
REMINDER_AT = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


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


def _task_detail(
    *,
    task_id: int = 1,
    series_id: int | None = None,
    title: str = "Task",
    completed_at: datetime | None = None,
    reminders_enabled: bool = True,
) -> TaskDetail:
    """Build a task detail with deterministic labels and reminders."""
    task = Task(
        id=task_id,
        user_id=1,
        series_id=series_id,
        project_id=None,
        title=title,
        description="Description",
        priority=Priority.URGENT_IMPORTANT.value,
        due_at=DUE_AT,
        completed_at=completed_at,
        reminders_enabled=reminders_enabled,
        created_at=NOW,
        updated_at=NOW,
    )
    label = Label(id=3, user_id=1, name="Home", color_id=1)
    reminder = TaskReminder(
        id=4,
        user_id=1,
        task_id=task_id,
        series_id=None,
        kind=ReminderKind.BEFORE_DUE.value,
        enabled=True,
        offset_minutes=30,
        interval_days=None,
        time_of_day=None,
        timezone=None,
        next_run_at=REMINDER_AT,
    )
    return TaskDetail(task=task, labels=[label], reminders=[reminder], project=None)


class FakeTaskService:
    """Task service double returning prepared details and recording calls."""

    def __init__(self) -> None:
        """Initialize empty state, prepared results, and call tracking."""
        self.details: dict[int, TaskDetail] = {}
        self.list_calls: list[tuple[int, TaskFilters]] = []
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[int, dict[str, Any]]] = []
        self.completed: list[int] = []
        self.uncompleted: list[int] = []
        self.deleted: list[int] = []
        self.create_result: TaskDetail | None = None
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.update_error: Exception | None = None
        self.delete_error: Exception | None = None

    def add_detail(self, detail: TaskDetail) -> None:
        """Store a prepared task detail."""
        self.details[detail.task.id] = detail

    async def list(self, user_id: int, filters: TaskFilters) -> list[TaskDetail]:
        """Record list filters and return prepared details."""
        self.list_calls.append((user_id, filters))
        return list(self.details.values())

    async def get(self, user_id: int, task_id: int) -> TaskDetail:
        """Return a prepared detail or raise the configured error."""
        if self.get_error is not None:
            raise self.get_error
        detail = self.details.get(task_id)
        if detail is None:
            raise NotFoundError("Task not found")
        return detail

    async def create(self, user_id: int, **kwargs: Any) -> TaskDetail:
        """Record creation arguments and return the prepared detail."""
        self.created.append({"user_id": user_id, **kwargs})
        if self.create_error is not None:
            raise self.create_error
        detail = self.create_result or _task_detail()
        self.details[detail.task.id] = detail
        return detail

    async def update(self, user_id: int, task_id: int, **changes: Any) -> TaskDetail:
        """Record update arguments and apply simple title changes."""
        self.updated.append((task_id, dict(changes)))
        if self.update_error is not None:
            raise self.update_error
        detail = self.details.get(task_id)
        if detail is None:
            raise NotFoundError("Task not found")
        title = changes.get("title")
        if isinstance(title, str):
            detail.task.title = title
        return detail

    async def complete(self, user_id: int, task_id: int) -> TaskDetail:
        """Mark a prepared task complete and return it."""
        self.completed.append(task_id)
        detail = self.details.get(task_id)
        if detail is None:
            raise NotFoundError("Task not found")
        detail.task.completed_at = NOW
        return detail

    async def uncomplete(self, user_id: int, task_id: int) -> TaskDetail:
        """Reopen a prepared task and return it."""
        self.uncompleted.append(task_id)
        detail = self.details.get(task_id)
        if detail is None:
            raise NotFoundError("Task not found")
        detail.task.completed_at = None
        return detail

    async def delete(self, user_id: int, task_id: int) -> None:
        """Record deletion or raise the configured error."""
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(task_id)
        self.details.pop(task_id, None)


@pytest.fixture
def fake_task_service() -> FakeTaskService:
    """Build a task service double with no prepared details."""
    return FakeTaskService()


@pytest.fixture
async def task_client(
    app: FastAPI,
    fake_task_service: FakeTaskService,
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated against the fake task service."""
    app.dependency_overrides[get_task_service] = lambda: fake_task_service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_create_task_returns_reminders_and_next_run(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Return reminder configuration and the computed next reminder slot."""
    payload = {
        "title": "Write report",
        "priority": "urgent_important",
        "due_at": "2026-11-04T15:00:00+00:00",
        "reminders_enabled": True,
        "reminders": [
            {"kind": "before_due", "offset_minutes": 30},
            {
                "kind": "scheduled",
                "interval_days": 1,
                "time_of_day": "09:00:00",
            },
        ],
    }

    response = await task_client.post("/api/v1/tasks", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Task"
    assert body["priority"] == "urgent_important"
    assert body["labels"] == [{"id": 3, "name": "Home"}]
    assert body["reminders"][0]["kind"] == "before_due"
    assert body["reminders"][0]["offset_minutes"] == 30
    assert body["reminders"][0]["next_run_at"] == REMINDER_AT.isoformat().replace("+00:00", "Z")
    recorded = fake_task_service.created[0]
    assert recorded["user_id"] == 1
    assert recorded["due_at"] == DUE_AT
    assert [item.kind for item in recorded["reminders"]] == [
        ReminderKind.BEFORE_DUE,
        ReminderKind.SCHEDULED,
    ]


async def test_list_tasks_passes_all_filters(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Translate every list query parameter into service filters."""
    fake_task_service.add_detail(_task_detail())

    response = await task_client.get(
        "/api/v1/tasks",
        params=[
            ("date", "2026-11-04"),
            ("project_ids", "1"),
            ("project_ids", "2"),
            ("priorities", "urgent_important"),
            ("priorities", "neither"),
            ("status", "overdue"),
            ("recurrence", "regular"),
            ("q", "report"),
            ("include_archived_projects", "true"),
        ],
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == 1
    user_id, filters = fake_task_service.list_calls[0]
    assert user_id == 1
    assert filters.date == date(2026, 11, 4)
    assert filters.project_ids == (1, 2)
    assert filters.priorities == (Priority.URGENT_IMPORTANT, Priority.NEITHER)
    assert filters.status == "overdue"
    assert filters.recurrence == "regular"
    assert filters.query == "report"
    assert filters.include_archived_projects is True


async def test_read_task_returns_detail(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Return one prepared task detail."""
    fake_task_service.add_detail(_task_detail())

    response = await task_client.get("/api/v1/tasks/1")

    assert response.status_code == 200
    assert response.json()["id"] == 1
    assert response.json()["reminders"][0]["id"] == 4


async def test_complete_and_uncomplete_return_updated_task(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Return the completed task and the reopened task."""
    fake_task_service.add_detail(_task_detail())

    completed = await task_client.post("/api/v1/tasks/1/complete")
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None
    assert fake_task_service.completed == [1]

    reopened = await task_client.post("/api/v1/tasks/1/uncomplete")
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None
    assert fake_task_service.uncompleted == [1]


async def test_update_series_instance_delegates_to_service(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Return the representative instance after a series-wide edit."""
    fake_task_service.add_detail(_task_detail(series_id=9))

    response = await task_client.patch(
        "/api/v1/tasks/1",
        json={"title": "Updated", "reminders": [{"kind": "before_due", "offset_minutes": 10}]},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Updated"
    assert response.json()["series_id"] == 9
    task_id, changes = fake_task_service.updated[0]
    assert task_id == 1
    assert changes["title"] == "Updated"
    assert [item.kind for item in changes["reminders"]] == [ReminderKind.BEFORE_DUE]


async def test_delete_task_returns_no_content(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Delete a task and return an empty 204 response."""
    fake_task_service.add_detail(_task_detail())

    response = await task_client.delete("/api/v1/tasks/1")

    assert response.status_code == 204
    assert response.content == b""
    assert fake_task_service.deleted == [1]


async def test_another_users_task_returns_not_found(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Hide another user's task behind a 404 not-found envelope."""
    fake_task_service.get_error = NotFoundError("Task not found")

    response = await task_client.get("/api/v1/tasks/999")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "not_found"
    assert body["message"] == "Task not found"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_create_task_rejects_naive_due_at(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Reject a due time without a timezone with a 422 envelope."""
    response = await task_client.post(
        "/api/v1/tasks",
        json={"title": "Task", "due_at": "2026-11-04T15:00:00"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert any(item["field"] == "body.due_at" for item in body["details"]["fields"])
    assert fake_task_service.created == []


@pytest.mark.parametrize(
    "reminders",
    [
        [{"kind": "before_due"}],
        [{"kind": "scheduled", "interval_days": 1}],
        [{"kind": "scheduled", "interval_days": 1, "time_of_day": "09:00", "offset_minutes": 5}],
        [{"kind": "before_due", "offset_minutes": 5, "interval_days": 1}],
        [
            {"kind": "before_due", "offset_minutes": 5},
            {"kind": "before_due", "offset_minutes": 10},
        ],
    ],
)
async def test_create_task_validates_reminder_invariants(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
    reminders: list[dict[str, object]],
) -> None:
    """Reject incomplete, inconsistent, or duplicated reminder rules."""
    response = await task_client.post(
        "/api/v1/tasks",
        json={
            "title": "Task",
            "due_at": "2026-11-04T15:00:00+00:00",
            "reminders": reminders,
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_task_service.created == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/tasks", None),
        ("post", "/api/v1/tasks", {"title": "Task", "due_at": "2026-11-04T15:00:00+00:00"}),
        ("get", "/api/v1/tasks/1", None),
        ("patch", "/api/v1/tasks/1", {"title": "Updated"}),
        ("post", "/api/v1/tasks/1/complete", None),
        ("post", "/api/v1/tasks/1/uncomplete", None),
        ("delete", "/api/v1/tasks/1", None),
    ],
)
async def test_tasks_require_bearer_token(
    client: AsyncClient,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    """Require a bearer token on every task route."""
    response = await client.request(method, path, json=payload)

    assert response.status_code == 401
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_task_list_accepts_empty_filters(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Use default filters when no query parameters are provided."""
    response = await task_client.get("/api/v1/tasks")

    assert response.status_code == 200
    filters = fake_task_service.list_calls[0][1]
    assert filters == TaskFilters()


async def test_scheduled_reminder_response_includes_timezone(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Expose the timezone of scheduled reminder responses."""
    detail = _task_detail()
    detail.reminders[0].kind = ReminderKind.SCHEDULED.value
    detail.reminders[0].offset_minutes = None
    detail.reminders[0].interval_days = 1
    detail.reminders[0].time_of_day = time(9, 0)
    detail.reminders[0].timezone = "Europe/Moscow"
    fake_task_service.add_detail(detail)

    response = await task_client.get("/api/v1/tasks/1")

    reminder = response.json()["reminders"][0]
    assert reminder["kind"] == "scheduled"
    assert reminder["timezone"] == "Europe/Moscow"
    assert reminder["time_of_day"] == "09:00:00"
