from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.deps import get_current_access, get_project_service
from backend.api.pagination import MAX_DB_ID, encode_cursor, filter_fingerprint
from backend.auth.tokens import AccessTokenClaims
from backend.core.errors import ConflictError, NotFoundError
from backend.domain.entities import ColorPalette, Project
from backend.domain.task_management.models import (
    ProjectListCursor,
    ProjectPage,
    ProjectProgress,
    ProjectSummary,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _cursor(order_key: list[object]) -> str:
    """Build a project-list cursor matching the default archive filter."""
    fingerprint = filter_fingerprint({"include_archived": False})
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


def _color(color_id: int = 1) -> ColorPalette:
    """Build a palette color used in prepared summaries."""
    return ColorPalette(
        id=color_id,
        slug="blue",
        name="Blue",
        hex="#0000FF",
        sort_order=1,
        is_active=True,
    )


def _summary(
    *,
    project_id: int = 1,
    user_id: int = 1,
    name: str = "Work",
    color: ColorPalette | None = None,
    total: int = 0,
    completed: int = 0,
    archived_at: datetime | None = None,
) -> ProjectSummary:
    """Build a project summary with progress and color data."""
    resolved_color = color or _color()
    project = Project(
        id=project_id,
        user_id=user_id,
        name=name,
        description="Plan",
        color_id=resolved_color.id,
        icon="briefcase",
        archived_at=archived_at,
        created_at=NOW,
        updated_at=NOW,
    )
    return ProjectSummary(
        project=project,
        progress=ProjectProgress(
            total=total,
            completed=completed,
            remaining=total - completed,
        ),
        color=resolved_color,
    )


class FakeProjectService:
    """Project service double backed by in-memory summaries."""

    def __init__(self) -> None:
        """Initialize empty project state and prepared errors."""
        self.summaries: dict[int, ProjectSummary] = {}
        self.colors: dict[int, ColorPalette] = {1: _color()}
        self.created: list[dict[str, object]] = []
        self.updated: list[tuple[int, dict[str, object]]] = []
        self.deleted: list[int] = []
        self.list_calls: list[tuple[int, bool]] = []
        self.summary_calls: list[tuple[int, int]] = []
        self.user_calls: list[tuple[str, int]] = []
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.update_error: Exception | None = None
        self.delete_error: Exception | None = None
        self._next_id = 2

    def add_summary(self, summary: ProjectSummary) -> None:
        """Store a prepared project summary."""
        self.summaries[summary.project.id] = summary

    async def list(
        self,
        user_id: int,
        include_archived: bool,
        *,
        limit: int,
        after: ProjectListCursor | None = None,
    ) -> ProjectPage:
        """Return one prepared page filtered, sorted, and cursor-bounded like the service."""
        self.list_calls.append((user_id, include_archived))
        self.user_calls.append(("list", user_id))
        summaries = sorted(
            (
                summary
                for summary in self.summaries.values()
                if summary.project.user_id == user_id
                and (include_archived or summary.project.archived_at is None)
            ),
            key=lambda summary: (
                summary.project.archived_at is not None,
                summary.project.name.lower(),
                summary.project.id,
            ),
        )
        if after is not None:
            summaries = [
                summary
                for summary in summaries
                if (
                    summary.project.archived_at is not None,
                    summary.project.name.lower(),
                    summary.project.id,
                )
                > (after.is_archived, after.name_key, after.project_id)
            ]
        page = summaries[:limit]
        next_cursor = (
            ProjectListCursor(
                is_archived=page[-1].project.archived_at is not None,
                name_key=page[-1].project.name.lower(),
                project_id=page[-1].project.id,
            )
            if len(summaries) > limit and page
            else None
        )
        return ProjectPage(items=page, next_cursor=next_cursor)

    async def get_summary(self, user_id: int, project_id: int) -> ProjectSummary:
        """Record the single-project lookup and return the prepared summary."""
        self.summary_calls.append((user_id, project_id))
        self.user_calls.append(("get_summary", user_id))
        summary = self.summaries.get(project_id)
        if summary is None or summary.project.user_id != user_id:
            raise NotFoundError("Project not found")
        return summary

    async def get(self, user_id: int, project_id: int) -> Project:
        """Return one prepared project or raise the configured error."""
        self.user_calls.append(("get", user_id))
        if self.get_error is not None:
            raise self.get_error
        summary = self.summaries.get(project_id)
        if summary is None or summary.project.user_id != user_id:
            raise NotFoundError("Project not found")
        return summary.project

    async def create(
        self,
        user_id: int,
        *,
        name: str,
        description: str | None = None,
        color_id: int | None = None,
        icon: str | None = None,
    ) -> Project:
        """Record creation arguments and store a new summary."""
        self.created.append(
            {
                "user_id": user_id,
                "name": name,
                "description": description,
                "color_id": color_id,
                "icon": icon,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        color = self.colors.get(color_id, _color()) if color_id is not None else _color()
        project = Project(
            id=self._next_id,
            user_id=user_id,
            name=name,
            description=description,
            color_id=color.id,
            icon=icon,
            archived_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        self._next_id += 1
        self.summaries[project.id] = ProjectSummary(
            project=project,
            progress=ProjectProgress(total=0, completed=0, remaining=0),
            color=color,
        )
        return project

    async def update(
        self,
        user_id: int,
        project_id: int,
        **changes: object,
    ) -> Project:
        """Record update arguments and apply the prepared changes."""
        self.user_calls.append(("update", user_id))
        self.updated.append((project_id, dict(changes)))
        if self.update_error is not None:
            raise self.update_error
        summary = self.summaries.get(project_id)
        if summary is None or summary.project.user_id != user_id:
            raise NotFoundError("Project not found")
        project = summary.project
        name = changes.get("name")
        if isinstance(name, str):
            project.name = name
        description = changes.get("description")
        if "description" in changes:
            project.description = description if isinstance(description, str) else None
        color_id = changes.get("color_id")
        if isinstance(color_id, int):
            project.color_id = color_id
        icon = changes.get("icon")
        if "icon" in changes:
            project.icon = icon if isinstance(icon, str) else None
        return project

    async def archive(self, user_id: int, project_id: int) -> Project:
        """Mark a prepared project as archived."""
        self.user_calls.append(("archive", user_id))
        summary = self.summaries.get(project_id)
        if summary is None or summary.project.user_id != user_id:
            raise NotFoundError("Project not found")
        summary.project.archived_at = NOW
        return summary.project

    async def unarchive(self, user_id: int, project_id: int) -> Project:
        """Return a prepared project to the active list."""
        self.user_calls.append(("unarchive", user_id))
        summary = self.summaries.get(project_id)
        if summary is None or summary.project.user_id != user_id:
            raise NotFoundError("Project not found")
        summary.project.archived_at = None
        return summary.project

    async def delete(self, user_id: int, project_id: int) -> None:
        """Record deletion or raise the configured error."""
        self.user_calls.append(("delete", user_id))
        if self.delete_error is not None:
            raise self.delete_error
        summary = self.summaries.get(project_id)
        if summary is None or summary.project.user_id != user_id:
            raise NotFoundError("Project not found")
        self.deleted.append(project_id)
        self.summaries.pop(project_id, None)


@pytest.fixture
def fake_project_service() -> FakeProjectService:
    """Build a project service double with no prepared projects."""
    return FakeProjectService()


@pytest.fixture
async def project_client(
    app: FastAPI,
    fake_project_service: FakeProjectService,
) -> AsyncIterator[AsyncClient]:
    """Yield a client authenticated against the fake project service."""
    app.dependency_overrides[get_project_service] = lambda: fake_project_service
    app.dependency_overrides[get_current_access] = lambda: _claims()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_list_projects_returns_progress_and_color(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Expose project progress counters and resolved color data."""
    fake_project_service.add_summary(_summary(total=5, completed=2))

    response = await project_client.get("/api/v1/projects")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == 1
    assert item["name"] == "Work"
    assert item["progress"] == {"total": 5, "completed": 2, "remaining": 3}
    assert item["color"] == {
        "id": 1,
        "slug": "blue",
        "name": "Blue",
        "hex": "#0000FF",
    }
    assert item["archived_at"] is None
    assert fake_project_service.list_calls == [(1, False)]


async def test_project_list_returns_default_page_size(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Return at most the default page size and a next-page cursor."""
    for index in range(51):
        fake_project_service.add_summary(
            _summary(project_id=index + 1, name=f"Project {index:02d}")
        )

    response = await project_client.get("/api/v1/projects")

    body = response.json()
    assert response.status_code == 200
    assert len(body["items"]) == 50
    assert body["next_cursor"] is not None


async def test_project_list_second_page_has_no_duplicates(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Continue after the last item of the first page without duplicates."""
    for index in range(51):
        fake_project_service.add_summary(
            _summary(project_id=index + 1, name=f"Project {index:02d}")
        )

    first = (await project_client.get("/api/v1/projects")).json()
    second = (
        await project_client.get("/api/v1/projects", params={"cursor": first["next_cursor"]})
    ).json()

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(range(1, 52))
    assert second["next_cursor"] is None


async def test_project_list_cursor_filter_mismatch_returns_400(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Reject a cursor issued for a different archive filter."""
    fake_project_service.add_summary(_summary(project_id=1, name="Alpha"))
    fake_project_service.add_summary(_summary(project_id=2, name="Beta"))

    first = (await project_client.get("/api/v1/projects", params={"limit": 1})).json()
    assert first["next_cursor"] is not None
    response = await project_client.get(
        "/api/v1/projects",
        params={
            "include_archived": True,
            "limit": 1,
            "cursor": first["next_cursor"],
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


async def test_project_list_equal_names_uses_id_tiebreaker(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Order rows with equal case-insensitive names by identifier ascending."""
    for project_id in (3, 1, 2):
        fake_project_service.add_summary(_summary(project_id=project_id, name="Work"))

    ids = [item["id"] for item in (await project_client.get("/api/v1/projects")).json()["items"]]

    assert ids == [1, 2, 3]


async def test_project_list_rejects_overlong_cursor(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Reject a cursor longer than the shared cursor limit as a bad request."""
    fake_project_service.add_summary(_summary())

    response = await project_client.get(
        "/api/v1/projects",
        params={"cursor": "x" * 513},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["message"] == "Invalid pagination cursor"


@pytest.mark.parametrize(
    "order_key",
    [
        [0, "alpha", MAX_DB_ID + 1],
        [0, "alpha\x00beta", 1],
        [0, "alpha\ud800", 1],
        [True, "alpha", 1],
    ],
)
async def test_project_list_rejects_unsafe_cursor_key(
    project_client: AsyncClient,
    order_key: list[object],
) -> None:
    """Reject out-of-range ids, unsafe name keys, and boolean archive flags."""
    response = await project_client.get(
        "/api/v1/projects",
        params={"cursor": _cursor(order_key)},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "bad_request"
    assert body["message"] == "Invalid pagination cursor"


async def test_project_list_openapi_documents_individual_params(app: FastAPI) -> None:
    """Document the archive filter and pagination parameters individually."""
    parameters = app.openapi()["paths"]["/api/v1/projects"]["get"]["parameters"]

    assert {parameter["name"] for parameter in parameters} == {
        "include_archived",
        "limit",
        "cursor",
    }


async def test_project_read_uses_single_summary(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Load a project detail through one summary lookup without listing projects."""
    fake_project_service.add_summary(_summary())

    response = await project_client.get("/api/v1/projects/1")

    assert response.status_code == 200
    assert fake_project_service.summary_calls == [(1, 1)]
    assert ("get", 1) not in fake_project_service.user_calls
    assert fake_project_service.list_calls == []


async def test_project_create_uses_single_summary(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Load a created project's response through one summary lookup."""
    response = await project_client.post("/api/v1/projects", json={"name": "Work"})

    assert response.status_code == 201
    assert fake_project_service.summary_calls == [(1, 2)]
    assert fake_project_service.list_calls == []


async def test_create_project_defaults_color(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Trim the name, delegate default color selection, and return 201."""
    response = await project_client.post("/api/v1/projects", json={"name": "  Work  "})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Work"
    assert body["color"]["id"] == 1
    assert body["progress"] == {"total": 0, "completed": 0, "remaining": 0}
    assert fake_project_service.created == [
        {
            "user_id": 1,
            "name": "Work",
            "description": None,
            "color_id": None,
            "icon": None,
        }
    ]


async def test_create_project_duplicate_name_returns_conflict(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Render a duplicate project name as a 409 conflict envelope."""
    fake_project_service.create_error = ConflictError("Project name already exists")

    response = await project_client.post("/api/v1/projects", json={"name": "Work"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "conflict"
    assert body["message"] == "Project name already exists"
    assert body["description"] == "Операция конфликтует с текущим состоянием"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "details" not in body


async def test_read_another_users_project_returns_not_found(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Hide another user's project behind a 404 not-found envelope."""
    fake_project_service.get_error = NotFoundError("Project not found")

    response = await project_client.get("/api/v1/projects/999")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "not_found"
    assert body["message"] == "Project not found"
    assert body["description"] == "Запрошенный ресурс не найден"
    assert body["request_id"] == response.headers["x-request-id"]


async def test_update_project_returns_updated_summary(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Apply a partial update and return the refreshed project summary."""
    fake_project_service.add_summary(_summary())

    response = await project_client.patch(
        "/api/v1/projects/1",
        json={"name": "Personal"},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Personal"
    assert fake_project_service.updated == [(1, {"name": "Personal"})]


async def test_archive_and_unarchive_change_active_list_visibility(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Hide an archived project from active lists and restore it on unarchive."""
    fake_project_service.add_summary(_summary())

    archived = await project_client.post("/api/v1/projects/1/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    active = await project_client.get("/api/v1/projects")
    assert active.json()["items"] == []

    archived_list = await project_client.get(
        "/api/v1/projects",
        params={"include_archived": True},
    )
    assert len(archived_list.json()["items"]) == 1

    unarchived = await project_client.post("/api/v1/projects/1/unarchive")
    assert unarchived.status_code == 200
    assert unarchived.json()["archived_at"] is None

    restored = await project_client.get("/api/v1/projects")
    assert len(restored.json()["items"]) == 1


async def test_delete_project_returns_no_content(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Delete a project and return an empty 204 response."""
    fake_project_service.add_summary(_summary())

    response = await project_client.delete("/api/v1/projects/1")

    assert response.status_code == 204
    assert response.content == b""
    assert fake_project_service.deleted == [1]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"name": "   "}, "body.name"),
        ({"name": "Work", "color_id": 0}, "body.color_id"),
    ],
)
async def test_create_project_validates_input(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
    payload: dict[str, object],
    field: str,
) -> None:
    """Reject blank names and non-positive color identifiers with 422."""
    response = await project_client.post("/api/v1/projects", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert any(item["field"] == field for item in body["details"]["fields"])
    assert fake_project_service.created == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/projects", None),
        ("post", "/api/v1/projects", {"name": "Work"}),
        ("get", "/api/v1/projects/1", None),
        ("patch", "/api/v1/projects/1", {"name": "Personal"}),
        ("post", "/api/v1/projects/1/archive", None),
        ("post", "/api/v1/projects/1/unarchive", None),
        ("delete", "/api/v1/projects/1", None),
    ],
)
async def test_projects_require_bearer_token(
    client: AsyncClient,
    method: str,
    path: str,
    payload: dict[str, object] | None,
) -> None:
    """Require a bearer token on every project route."""
    response = await client.request(method, path, json=payload)

    assert response.status_code == 401
    body = response.json()
    assert body["error"] is True
    assert body["code"] == "unauthorized"
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["www-authenticate"] == "Bearer"


async def test_update_project_clears_description_and_icon_with_explicit_null(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Explicit nulls clear nullable project fields instead of being omitted."""
    fake_project_service.add_summary(_summary())

    description = await project_client.patch(
        "/api/v1/projects/1",
        json={"description": None},
    )
    icon = await project_client.patch("/api/v1/projects/1", json={"icon": None})

    assert description.status_code == 200
    assert description.json()["description"] is None
    assert icon.status_code == 200
    assert icon.json()["icon"] is None
    assert fake_project_service.updated == [
        (1, {"description": None}),
        (1, {"icon": None}),
    ]


async def test_create_project_rejects_too_long_description(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Reject a description longer than 5000 characters on creation."""
    response = await project_client.post(
        "/api/v1/projects",
        json={"name": "Work", "description": "x" * 5001},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_project_service.created == []


async def test_update_project_rejects_too_long_description(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Reject a description longer than 5000 characters on update."""
    fake_project_service.add_summary(_summary())

    response = await project_client.patch(
        "/api/v1/projects/1",
        json={"description": "x" * 5001},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_project_service.updated == []


@pytest.mark.parametrize("payload", [{"name": "Work", "icon": "x" * 33}])
async def test_create_project_rejects_long_icon(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
    payload: dict[str, object],
) -> None:
    """Reject an icon longer than the database column on create."""
    response = await project_client.post("/api/v1/projects", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_project_service.created == []


async def test_update_project_rejects_long_icon(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """Reject an icon longer than the database column on update."""
    fake_project_service.add_summary(_summary())

    response = await project_client.patch(
        "/api/v1/projects/1",
        json={"icon": "x" * 33},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fake_project_service.updated == []


async def test_update_another_users_project_returns_not_found(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """A cross-user project update is reported as not found."""
    fake_project_service.add_summary(_summary(user_id=2))

    response = await project_client.patch(
        "/api/v1/projects/1",
        json={"name": "Personal"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_delete_another_users_project_returns_not_found(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """A cross-user project delete is reported as not found."""
    fake_project_service.add_summary(_summary(user_id=2))

    response = await project_client.delete("/api/v1/projects/1")

    assert response.status_code == 404
    assert fake_project_service.deleted == []


async def test_project_routes_forward_the_claim_user_id(
    project_client: AsyncClient,
    fake_project_service: FakeProjectService,
) -> None:
    """List, get, update, and delete pass the authenticated user identifier."""
    fake_project_service.add_summary(_summary())

    await project_client.get("/api/v1/projects")
    await project_client.get("/api/v1/projects/1")
    await project_client.patch("/api/v1/projects/1", json={"name": "Personal"})
    await project_client.delete("/api/v1/projects/1")

    assert ("list", 1) in fake_project_service.user_calls
    assert ("get_summary", 1) in fake_project_service.user_calls
    assert ("get", 1) not in fake_project_service.user_calls
    assert ("update", 1) in fake_project_service.user_calls
    assert ("delete", 1) in fake_project_service.user_calls
