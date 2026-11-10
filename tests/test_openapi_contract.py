"""Locked-down baseline of the public HTTP and OpenAPI contract."""

from __future__ import annotations

from backend.core.config import Settings
from backend.main import create_app

HTTP_METHODS = frozenset({"get", "post", "patch", "delete", "put"})

_BASE_ERROR_STATUSES = frozenset({"400", "404", "422", "500", "503"})
_AUTH_ERROR_STATUSES = _BASE_ERROR_STATUSES | {"401"}
_CONFLICT_ERROR_STATUSES = _AUTH_ERROR_STATUSES | {"409"}

EXPECTED_PATHS = frozenset(
    {
        "/api/v1/auth/logout",
        "/api/v1/auth/me",
        "/api/v1/auth/me/timezone",
        "/api/v1/auth/refresh",
        "/api/v1/auth/telegram",
        "/api/v1/color-palette",
        "/api/v1/labels",
        "/api/v1/labels/{label_id}",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}",
        "/api/v1/projects/{project_id}/archive",
        "/api/v1/projects/{project_id}/unarchive",
        "/api/v1/task-series",
        "/api/v1/task-series/{series_id}",
        "/api/v1/tasks",
        "/api/v1/tasks/today/progress",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/complete",
        "/api/v1/tasks/{task_id}/uncomplete",
        "/health/live",
        "/health/ready",
    }
)

EXPECTED_OPERATION_IDS = {
    "delete /api/v1/labels/{label_id}": "labels-delete_label",
    "delete /api/v1/projects/{project_id}": "projects-delete_project",
    "delete /api/v1/task-series/{series_id}": "task-series-delete_series",
    "delete /api/v1/tasks/{task_id}": "tasks-delete_task",
    "get /api/v1/auth/me": "auth-read_current_user",
    "get /api/v1/color-palette": "color-palette-read_color_palette",
    "get /api/v1/labels": "labels-list_labels",
    "get /api/v1/projects": "projects-list_projects",
    "get /api/v1/projects/{project_id}": "projects-read_project",
    "get /api/v1/task-series": "task-series-list_series",
    "get /api/v1/task-series/{series_id}": "task-series-read_series",
    "get /api/v1/tasks": "tasks-list_tasks",
    "get /api/v1/tasks/today/progress": "tasks-read_today_progress",
    "get /api/v1/tasks/{task_id}": "tasks-read_task",
    "get /health/live": "health-live",
    "get /health/ready": "health-ready",
    "patch /api/v1/auth/me/timezone": "auth-update_timezone",
    "patch /api/v1/labels/{label_id}": "labels-update_label",
    "patch /api/v1/projects/{project_id}": "projects-update_project",
    "patch /api/v1/task-series/{series_id}": "task-series-update_series",
    "patch /api/v1/tasks/{task_id}": "tasks-update_task",
    "post /api/v1/auth/logout": "auth-logout",
    "post /api/v1/auth/refresh": "auth-refresh_tokens",
    "post /api/v1/auth/telegram": "auth-telegram_login",
    "post /api/v1/labels": "labels-create_label",
    "post /api/v1/projects": "projects-create_project",
    "post /api/v1/projects/{project_id}/archive": "projects-archive_project",
    "post /api/v1/projects/{project_id}/unarchive": "projects-unarchive_project",
    "post /api/v1/task-series": "task-series-create_series",
    "post /api/v1/tasks": "tasks-create_task",
    "post /api/v1/tasks/{task_id}/complete": "tasks-complete_task",
    "post /api/v1/tasks/{task_id}/uncomplete": "tasks-uncomplete_task",
}

EXPECTED_ERROR_STATUSES = {
    "delete /api/v1/labels/{label_id}": _CONFLICT_ERROR_STATUSES,
    "delete /api/v1/projects/{project_id}": _CONFLICT_ERROR_STATUSES,
    "delete /api/v1/task-series/{series_id}": _AUTH_ERROR_STATUSES,
    "delete /api/v1/tasks/{task_id}": _AUTH_ERROR_STATUSES,
    "get /api/v1/auth/me": _AUTH_ERROR_STATUSES,
    "get /api/v1/color-palette": _CONFLICT_ERROR_STATUSES,
    "get /api/v1/labels": _CONFLICT_ERROR_STATUSES,
    "get /api/v1/projects": _CONFLICT_ERROR_STATUSES,
    "get /api/v1/projects/{project_id}": _CONFLICT_ERROR_STATUSES,
    "get /api/v1/task-series": _AUTH_ERROR_STATUSES,
    "get /api/v1/task-series/{series_id}": _AUTH_ERROR_STATUSES,
    "get /api/v1/tasks": _AUTH_ERROR_STATUSES,
    "get /api/v1/tasks/today/progress": _AUTH_ERROR_STATUSES,
    "get /api/v1/tasks/{task_id}": _AUTH_ERROR_STATUSES,
    "get /health/live": _BASE_ERROR_STATUSES,
    "get /health/ready": _BASE_ERROR_STATUSES,
    "patch /api/v1/auth/me/timezone": _AUTH_ERROR_STATUSES,
    "patch /api/v1/labels/{label_id}": _CONFLICT_ERROR_STATUSES,
    "patch /api/v1/projects/{project_id}": _CONFLICT_ERROR_STATUSES,
    "patch /api/v1/task-series/{series_id}": _AUTH_ERROR_STATUSES,
    "patch /api/v1/tasks/{task_id}": _AUTH_ERROR_STATUSES,
    "post /api/v1/auth/logout": _BASE_ERROR_STATUSES | {"403"},
    "post /api/v1/auth/refresh": _AUTH_ERROR_STATUSES | {"403"},
    "post /api/v1/auth/telegram": _AUTH_ERROR_STATUSES | {"403"},
    "post /api/v1/labels": _CONFLICT_ERROR_STATUSES,
    "post /api/v1/projects": _CONFLICT_ERROR_STATUSES,
    "post /api/v1/projects/{project_id}/archive": _CONFLICT_ERROR_STATUSES,
    "post /api/v1/projects/{project_id}/unarchive": _CONFLICT_ERROR_STATUSES,
    "post /api/v1/task-series": _AUTH_ERROR_STATUSES,
    "post /api/v1/tasks": _AUTH_ERROR_STATUSES,
    "post /api/v1/tasks/{task_id}/complete": _AUTH_ERROR_STATUSES,
    "post /api/v1/tasks/{task_id}/uncomplete": _AUTH_ERROR_STATUSES,
}


def test_openapi_surface_is_frozen() -> None:
    """Paths, methods, operation IDs, and documented error statuses stay stable."""
    app = create_app(Settings(_env_file=None, app_env="local", log_json=False))
    spec = app.openapi()

    assert set(spec["paths"]) == EXPECTED_PATHS

    actual_operation_ids = {
        f"{method} {path}": operation["operationId"]
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if method in HTTP_METHODS
    }
    assert actual_operation_ids == EXPECTED_OPERATION_IDS

    for key, statuses in EXPECTED_ERROR_STATUSES.items():
        method, path = key.split(" ", 1)
        documented = set(spec["paths"][path][method]["responses"])
        assert documented >= statuses
