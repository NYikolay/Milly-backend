from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_current_access, get_task_service
from backend.api.pagination import MAX_DB_ID, encode_cursor, filter_fingerprint
from backend.auth.tokens import AccessTokenClaims
from backend.core.errors import NotFoundError
from backend.models.label import Label
from backend.models.task import Task
from backend.models.task_reminder import TaskReminder
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.series import SeriesService
from backend.task_management.tasks import (
    TaskDetail,
    TaskFilters,
    TaskListCursor,
    TaskPage,
    TaskService,
)
from tests.task_management_fakes import FakeTaskManagementRepository, fixed_now

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
REMINDER_AT = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


def _cursor(order_key: list[object]) -> str:
    """Build a task-list cursor matching the default list filters."""
    fingerprint = filter_fingerprint(
        {
            "date": None,
            "include_archived_projects": False,
            "priorities": [],
            "project_ids": [],
            "q": None,
            "recurrence": "all",
            "status": "all",
        }
    )
    return encode_cursor(user_id=1, filter_fingerprint=fingerprint, order_key=order_key)


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


def _task_detail(
    *,
    task_id: int = 1,
    user_id: int = 1,
    series_id: int | None = None,
    title: str = "Task",
    due_at: datetime = DUE_AT,
    completed_at: datetime | None = None,
    reminders_enabled: bool = True,
) -> TaskDetail:
    """Build a task detail with deterministic labels and reminders."""
    task = Task(
        id=task_id,
        user_id=user_id,
        series_id=series_id,
        project_id=None,
        title=title,
        description="Description",
        priority=Priority.URGENT_IMPORTANT.value,
        due_at=due_at,
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
        self.user_calls: list[tuple[str, int]] = []
        self.create_result: TaskDetail | None = None
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.update_error: Exception | None = None
        self.delete_error: Exception | None = None

    def add_detail(self, detail: TaskDetail) -> None:
        """Store a prepared task detail."""
        self.details[detail.task.id] = detail

    async def list(
        self,
        user_id: int,
        filters: TaskFilters,
        *,
        limit: int,
        after: TaskListCursor | None = None,
    ) -> TaskPage:
        """Record list filters and return the requested prepared page."""
        self.list_calls.append((user_id, filters))
        self.user_calls.append(("list", user_id))
        details = sorted(
            self.details.values(),
            key=lambda detail: (detail.task.due_at, detail.task.id),
        )
        if after is not None:
            details = [
                detail
                for detail in details
                if detail.task.due_at > after.due_at
                or (detail.task.due_at == after.due_at and detail.task.id > after.task_id)
            ]
        page = details[:limit]
        next_cursor = (
            TaskListCursor(due_at=page[-1].task.due_at, task_id=page[-1].task.id)
            if len(details) > limit and page
            else None
        )
        return TaskPage(items=page, next_cursor=next_cursor)

    async def get(self, user_id: int, task_id: int) -> TaskDetail:
        """Return a prepared detail or raise the configured error."""
        self.user_calls.append(("get", user_id))
        if self.get_error is not None:
            raise self.get_error
        detail = self.details.get(task_id)
        if detail is None or detail.task.user_id != user_id:
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
        """Record update arguments and apply simple field changes."""
        self.user_calls.append(("update", user_id))
        self.updated.append((task_id, dict(changes)))
        if self.update_error is not None:
            raise self.update_error
        detail = self.details.get(task_id)
        if detail is None or detail.task.user_id != user_id:
            raise NotFoundError("Task not found")
        title = changes.get("title")
        if isinstance(title, str):
            detail.task.title = title
        if "description" in changes:
            detail.task.description = changes["description"]
        if "project_id" in changes:
            detail.task.project_id = changes["project_id"]
        return detail

    async def complete(self, user_id: int, task_id: int) -> TaskDetail:
        """Mark a prepared task complete and return it."""
        self.user_calls.append(("complete", user_id))
        self.completed.append(task_id)
        detail = self.details.get(task_id)
        if detail is None or detail.task.user_id != user_id:
            raise NotFoundError("Task not found")
        detail.task.completed_at = NOW
        return detail

    async def uncomplete(self, user_id: int, task_id: int) -> TaskDetail:
        """Reopen a prepared task and return it."""
        self.user_calls.append(("uncomplete", user_id))
        self.uncompleted.append(task_id)
        detail = self.details.get(task_id)
        if detail is None or detail.task.user_id != user_id:
            raise NotFoundError("Task not found")
        detail.task.completed_at = None
        return detail

    async def delete(self, user_id: int, task_id: int) -> None:
        """Record deletion or raise the configured error."""
        self.user_calls.append(("delete", user_id))
        if self.delete_error is not None:
            raise self.delete_error
        detail = self.details.get(task_id)
        if detail is None or detail.task.user_id != user_id:
            raise NotFoundError("Task not found")
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


async def test_task_create_rejects_too_many_labels(task_client: AsyncClient) -> None:
    """Reject more than 100 label identifiers on task creation."""
    response = await task_client.post(
        "/api/v1/tasks",
        json={
            "title": "Many",
            "due_at": "2026-11-04T09:00:00Z",
            "label_ids": list(range(1, 102)),
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_task_update_rejects_too_many_labels(task_client: AsyncClient) -> None:
    """Reject more than 100 label identifiers on task update."""
    response = await task_client.patch(
        "/api/v1/tasks/1",
        json={"label_ids": list(range(1, 102))},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


async def test_task_create_rejects_too_long_description(task_client: AsyncClient) -> None:
    """Reject a description longer than 5000 characters on task creation."""
    response = await task_client.post(
        "/api/v1/tasks",
        json={
            "title": "Long",
            "due_at": "2026-11-04T09:00:00Z",
            "description": "x" * 5001,
        },
    )

    assert response.status_code == 422


async def test_task_update_rejects_too_long_description(task_client: AsyncClient) -> None:
    """Reject a description longer than 5000 characters on task update."""
    response = await task_client.patch(
        "/api/v1/tasks/1",
        json={"description": "x" * 5001},
    )

    assert response.status_code == 422


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


async def test_task_list_returns_default_page_size(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Return at most the default page size and a next-page cursor."""
    for index in range(51):
        fake_task_service.add_detail(
            _task_detail(task_id=index + 1, due_at=NOW + timedelta(minutes=index))
        )

    response = await task_client.get("/api/v1/tasks")

    body = response.json()
    assert response.status_code == 200
    assert len(body["items"]) == 50
    assert body["next_cursor"] is not None


async def test_task_list_second_page_has_no_duplicates(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Continue after the last item of the first page without duplicates."""
    for index in range(51):
        fake_task_service.add_detail(
            _task_detail(task_id=index + 1, due_at=NOW + timedelta(minutes=index))
        )

    first = (await task_client.get("/api/v1/tasks")).json()
    second = (
        await task_client.get("/api/v1/tasks", params={"cursor": first["next_cursor"]})
    ).json()

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(range(1, 52))
    assert second["next_cursor"] is None


async def test_task_list_rejects_filter_mismatch_cursor(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Reject a cursor issued for a different filter set."""
    fake_task_service.add_detail(_task_detail(task_id=1, due_at=NOW))
    fake_task_service.add_detail(_task_detail(task_id=2, due_at=NOW + timedelta(minutes=1)))

    first = (await task_client.get("/api/v1/tasks", params={"status": "open", "limit": 1})).json()
    assert first["next_cursor"] is not None
    response = await task_client.get(
        "/api/v1/tasks",
        params={"status": "completed", "limit": 1, "cursor": first["next_cursor"]},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


async def test_task_list_rejects_invalid_cursor(task_client: AsyncClient) -> None:
    """Reject a cursor that is not an opaque pagination token."""
    response = await task_client.get("/api/v1/tasks", params={"cursor": "not-base64"})

    assert response.status_code == 400


async def test_task_list_rejects_overlong_cursor(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Reject a cursor longer than the shared cursor limit as a bad request."""
    fake_task_service.add_detail(_task_detail())

    response = await task_client.get(
        "/api/v1/tasks",
        params={"cursor": "x" * 513},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["message"] == "Invalid pagination cursor"


async def test_task_list_rejects_task_id_out_of_database_range(
    task_client: AsyncClient,
) -> None:
    """Reject a crafted cursor whose task identifier exceeds the bigint range."""
    response = await task_client.get(
        "/api/v1/tasks",
        params={"cursor": _cursor([DUE_AT.isoformat(), MAX_DB_ID + 1])},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["message"] == "Invalid pagination cursor"


async def test_task_list_openapi_documents_individual_filters(app: FastAPI) -> None:
    """Document every task-list filter and pagination parameter individually."""
    parameters = app.openapi()["paths"]["/api/v1/tasks"]["get"]["parameters"]

    assert {parameter["name"] for parameter in parameters} == {
        "date",
        "project_ids",
        "priorities",
        "status",
        "recurrence",
        "q",
        "include_archived_projects",
        "limit",
        "cursor",
    }


async def test_task_list_equal_due_at_uses_id_tiebreaker(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Order rows with equal due times by identifier ascending."""
    due_at = NOW
    for task_id in (3, 1, 2):
        fake_task_service.add_detail(_task_detail(task_id=task_id, due_at=due_at))

    ids = [item["id"] for item in (await task_client.get("/api/v1/tasks")).json()["items"]]

    assert ids == [1, 2, 3]


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


async def test_update_task_clears_description_and_project_with_explicit_null(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Explicit nulls clear nullable task fields instead of being omitted."""
    fake_task_service.add_detail(_task_detail())

    description = await task_client.patch("/api/v1/tasks/1", json={"description": None})
    project = await task_client.patch("/api/v1/tasks/1", json={"project_id": None})

    assert description.status_code == 200
    assert description.json()["description"] is None
    assert project.status_code == 200
    assert project.json()["project_id"] is None
    assert fake_task_service.updated == [
        (1, {"description": None}),
        (1, {"project_id": None}),
    ]


@pytest.mark.parametrize("method", ["post", "patch"])
async def test_task_routes_reject_aware_reminder_time(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
    method: str,
) -> None:
    """Reject a timezone-aware reminder wall-clock time with 422."""
    reminder = {
        "kind": "scheduled",
        "interval_days": 1,
        "time_of_day": "09:00:00+03:00",
    }
    if method == "post":
        response = await task_client.post(
            "/api/v1/tasks",
            json={
                "title": "Task",
                "due_at": "2026-11-04T15:00:00+00:00",
                "reminders": [reminder],
            },
        )
    else:
        fake_task_service.add_detail(_task_detail())
        response = await task_client.patch(
            "/api/v1/tasks/1",
            json={"reminders": [reminder]},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert fake_task_service.created == []
    assert fake_task_service.updated == []


@pytest.mark.parametrize(
    "reminder",
    [
        {"kind": "before_due", "offset_minutes": 525_601},
        {"kind": "scheduled", "interval_days": 32_768, "time_of_day": "09:00:00"},
    ],
)
async def test_create_task_rejects_out_of_range_reminder_numbers(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
    reminder: dict[str, object],
) -> None:
    """Reject numeric reminder values outside the database-safe ranges."""
    response = await task_client.post(
        "/api/v1/tasks",
        json={
            "title": "Task",
            "due_at": "2026-11-04T15:00:00+00:00",
            "reminders": [reminder],
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_task_service.created == []


async def test_create_task_rejects_due_at_before_minimum(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Reject a due instant before the supported UTC lower bound."""
    response = await task_client.post(
        "/api/v1/tasks",
        json={
            "title": "Old",
            "due_at": "2024-12-31T23:59:59Z",
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert fake_task_service.created == []


async def test_update_another_users_task_returns_not_found(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """A cross-user task update is reported as not found."""
    fake_task_service.add_detail(_task_detail(user_id=2))

    response = await task_client.patch("/api/v1/tasks/1", json={"title": "Changed"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_delete_another_users_task_returns_not_found(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """A cross-user task delete is reported as not found."""
    fake_task_service.add_detail(_task_detail(user_id=2))

    response = await task_client.delete("/api/v1/tasks/1")

    assert response.status_code == 404
    assert fake_task_service.deleted == []


async def test_task_routes_forward_the_claim_user_id(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """List, get, update, and delete pass the authenticated user identifier."""
    fake_task_service.add_detail(_task_detail())

    await task_client.get("/api/v1/tasks")
    await task_client.get("/api/v1/tasks/1")
    await task_client.patch("/api/v1/tasks/1", json={"title": "Changed"})
    await task_client.delete("/api/v1/tasks/1")

    assert ("list", 1) in fake_task_service.user_calls
    assert ("get", 1) in fake_task_service.user_calls
    assert ("update", 1) in fake_task_service.user_calls
    assert ("delete", 1) in fake_task_service.user_calls


async def test_task_list_rejects_max_date_with_validation_error(app: FastAPI) -> None:
    """The last calendar date is rejected before it can overflow local bounds."""
    repository = FakeTaskManagementRepository()
    palette = PaletteService(repository)
    series = SeriesService(
        repository,
        ProjectService(repository, palette, clock=fixed_now),
        LabelService(repository, palette, clock=fixed_now),
        clock=fixed_now,
    )
    service = TaskService(repository, series, clock=fixed_now)
    app.dependency_overrides[get_task_service] = lambda: service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/tasks", params={"date": "9999-12-31"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_task_list_rejects_min_date_in_positive_offset_zone(app: FastAPI) -> None:
    """The first calendar date is rejected before it can overflow local bounds."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[1] = "Europe/Moscow"
    palette = PaletteService(repository)
    series = SeriesService(
        repository,
        ProjectService(repository, palette, clock=fixed_now),
        LabelService(repository, palette, clock=fixed_now),
        clock=fixed_now,
    )
    service = TaskService(repository, series, clock=fixed_now)
    app.dependency_overrides[get_task_service] = lambda: service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/tasks", params={"date": "0001-01-01"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == response.headers["x-request-id"]
