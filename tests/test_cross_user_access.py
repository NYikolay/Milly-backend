from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from backend.api.deps import (
    get_current_access,
    get_label_service,
    get_project_service,
    get_series_service,
    get_task_service,
)
from backend.application.task_management.task_details import TaskDetails
from backend.application.task_management.task_references import TaskReferences
from backend.auth.tokens import AccessTokenClaims
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.series import SeriesService
from backend.task_management.tasks import TaskService
from tests.task_management_fakes import (
    FakeTaskManagementRepository,
    make_label,
    make_project,
    make_series,
    make_task,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
OWNER_ID = 1
OTHER_USER_ID = 2


def _claims(user_id: int = OWNER_ID) -> AccessTokenClaims:
    """Build verified access claims for the requesting user."""
    return AccessTokenClaims(
        user_id=user_id,
        session_id=77,
        telegram_id=123,
        jti="test-jti",
        issued_at=NOW,
        expires_at=NOW.replace(hour=12, minute=15),
    )


def _snapshot(repository: FakeTaskManagementRepository) -> dict[str, object]:
    """
    Capture the mutable task-management state of the in-memory repository.

    Purpose:
        Prove that a rejected cross-user request performed no partial writes.

    Args:
        repository: In-memory repository used by the API under test.

    Returns:
        dict[str, object]: Hashable snapshots of tasks, series, projects,
            labels, and label assignments.
    """
    return {
        "tasks": {
            task_id: (
                task.user_id,
                task.project_id,
                task.title,
                task.completed_at,
                task.deleted_at,
            )
            for task_id, task in repository.tasks.items()
        },
        "series": {
            series_id: (
                series.user_id,
                series.project_id,
                series.title,
                series.deleted_at,
            )
            for series_id, series in repository.series.items()
        },
        "projects": {
            project_id: (
                project.user_id,
                project.name,
                project.archived_at,
                project.deleted_at,
            )
            for project_id, project in repository.projects.items()
        },
        "labels": {
            label_id: (label.user_id, label.name, label.deleted_at)
            for label_id, label in repository.labels.items()
        },
        "task_labels": set(repository.task_labels),
        "series_labels": set(repository.series_labels),
    }


def _assert_not_found(response: Response) -> None:
    """Assert the unified not-found envelope for a rejected request."""
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["description"] == "Запрошенный ресурс не найден"


@pytest.fixture
def repository() -> FakeTaskManagementRepository:
    """Build a repository with one resource set owned by each of two users."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[OWNER_ID] = "UTC"
    repository.user_timezones[OTHER_USER_ID] = "UTC"
    repository.insert_project(make_project(project_id=1, user_id=OTHER_USER_ID, name="Foreign"))
    repository.insert_project(make_project(project_id=2, user_id=OWNER_ID, name="Mine"))
    repository.insert_label(make_label(label_id=1, user_id=OTHER_USER_ID, name="Foreign"))
    repository.insert_label(make_label(label_id=2, user_id=OWNER_ID, name="Mine"))
    repository.insert_task(
        make_task(
            task_id=1,
            user_id=OTHER_USER_ID,
            project_id=1,
            title="Foreign",
            due_at=DUE_AT,
        )
    )
    repository.insert_task(
        make_task(
            task_id=2,
            user_id=OWNER_ID,
            project_id=2,
            title="Mine",
            due_at=DUE_AT,
        )
    )
    repository.insert_series(
        make_series(
            series_id=1,
            user_id=OTHER_USER_ID,
            project_id=1,
            title="Foreign",
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=date(2026, 11, 4),
            generated_through=NOW,
        )
    )
    repository.insert_series(
        make_series(
            series_id=2,
            user_id=OWNER_ID,
            project_id=2,
            title="Mine",
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=date(2026, 11, 4),
            generated_through=NOW,
        )
    )
    return repository


@pytest.fixture
def palette_service(repository: FakeTaskManagementRepository) -> PaletteService:
    """Build a palette service backed by the in-memory repository."""
    return PaletteService(repository)


@pytest.fixture
def project_service(
    repository: FakeTaskManagementRepository,
    palette_service: PaletteService,
) -> ProjectService:
    """Build a project service backed by the in-memory repository."""
    return ProjectService(repository, palette_service, repository, clock=lambda: NOW)


@pytest.fixture
def label_service(
    repository: FakeTaskManagementRepository,
    palette_service: PaletteService,
) -> LabelService:
    """Build a label service backed by the in-memory repository."""
    return LabelService(repository, palette_service, clock=lambda: NOW)


@pytest.fixture
def series_service(repository: FakeTaskManagementRepository) -> SeriesService:
    """Build a series service backed by the in-memory repository."""
    return SeriesService(
        repository,
        repository,
        repository,
        repository,
        TaskReferences(repository, repository, repository),
        repository,
        clock=lambda: NOW,
    )


@pytest.fixture
def task_service(
    repository: FakeTaskManagementRepository,
    series_service: SeriesService,
) -> TaskService:
    """Build a task service backed by the in-memory repository."""
    return TaskService(
        repository,
        repository,
        repository,
        repository,
        TaskReferences(repository, repository, repository),
        TaskDetails(repository, repository, repository),
        series_service,
        repository,
        clock=lambda: NOW,
    )


@pytest.fixture
async def user_one_client(
    app: FastAPI,
    task_service: TaskService,
    series_service: SeriesService,
    project_service: ProjectService,
    label_service: LabelService,
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated as the first user against real services."""
    app.dependency_overrides[get_current_access] = lambda: _claims(OWNER_ID)
    app.dependency_overrides[get_task_service] = lambda: task_service
    app.dependency_overrides[get_series_service] = lambda: series_service
    app.dependency_overrides[get_project_service] = lambda: project_service
    app.dependency_overrides[get_label_service] = lambda: label_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_create_task_with_foreign_project_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a task creation that references another user's project."""
    before = _snapshot(repository)

    response = await user_one_client.post(
        "/api/v1/tasks",
        json={
            "title": "New",
            "due_at": "2026-11-04T15:00:00Z",
            "project_id": 1,
        },
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_create_task_with_foreign_labels_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a task creation that references another user's labels."""
    before = _snapshot(repository)

    response = await user_one_client.post(
        "/api/v1/tasks",
        json={
            "title": "New",
            "due_at": "2026-11-04T15:00:00Z",
            "label_ids": [1],
        },
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_create_series_with_foreign_project_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a series creation that references another user's project."""
    before = _snapshot(repository)

    response = await user_one_client.post(
        "/api/v1/task-series",
        json={
            "title": "New",
            "time_of_day": "09:00:00",
            "timezone": "UTC",
            "starts_on": "2026-11-04",
            "project_id": 1,
        },
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_create_series_with_foreign_labels_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a series creation that references another user's labels."""
    before = _snapshot(repository)

    response = await user_one_client.post(
        "/api/v1/task-series",
        json={
            "title": "New",
            "time_of_day": "09:00:00",
            "timezone": "UTC",
            "starts_on": "2026-11-04",
            "label_ids": [1],
        },
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_update_task_with_foreign_project_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a task update that references another user's project."""
    before = _snapshot(repository)

    response = await user_one_client.patch(
        "/api/v1/tasks/2",
        json={"project_id": 1},
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_update_task_with_foreign_labels_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a task update that references another user's labels."""
    before = _snapshot(repository)

    response = await user_one_client.patch(
        "/api/v1/tasks/2",
        json={"label_ids": [1]},
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_update_series_with_foreign_project_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a series update that references another user's project."""
    before = _snapshot(repository)

    response = await user_one_client.patch(
        "/api/v1/task-series/2",
        json={"project_id": 1},
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_update_series_with_foreign_labels_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject a series update that references another user's labels."""
    before = _snapshot(repository)

    response = await user_one_client.patch(
        "/api/v1/task-series/2",
        json={"label_ids": [1]},
    )

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_complete_foreign_task_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject completion of another user's task without mutation."""
    before = _snapshot(repository)

    response = await user_one_client.post("/api/v1/tasks/1/complete")

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_uncomplete_foreign_task_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject reopening another user's task without mutation."""
    before = _snapshot(repository)

    response = await user_one_client.post("/api/v1/tasks/1/uncomplete")

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_archive_foreign_project_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject archiving another user's project without mutation."""
    before = _snapshot(repository)

    response = await user_one_client.post("/api/v1/projects/1/archive")

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_unarchive_foreign_project_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject unarchiving another user's project without mutation."""
    before = _snapshot(repository)

    response = await user_one_client.post("/api/v1/projects/1/unarchive")

    _assert_not_found(response)
    assert _snapshot(repository) == before


async def test_delete_foreign_task_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject deleting another user's task and leave it intact."""
    before = _snapshot(repository)

    response = await user_one_client.delete("/api/v1/tasks/1")

    _assert_not_found(response)
    assert _snapshot(repository) == before
    assert repository.tasks[1].deleted_at is None


async def test_delete_foreign_series_returns_not_found(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Reject deleting another user's series and leave it intact."""
    before = _snapshot(repository)

    response = await user_one_client.delete("/api/v1/task-series/1")

    _assert_not_found(response)
    assert _snapshot(repository) == before
    assert repository.series[1].deleted_at is None


async def test_delete_foreign_label_is_a_noop(
    user_one_client: AsyncClient,
    repository: FakeTaskManagementRepository,
) -> None:
    """Keep another user's label intact and return the idempotent 204."""
    response = await user_one_client.delete("/api/v1/labels/1")

    assert response.status_code == 204
    assert repository.labels[1].deleted_at is None
