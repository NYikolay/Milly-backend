"""Locked-down baseline of the public HTTP and OpenAPI contract."""

from __future__ import annotations

from typing import Any

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

EXPECTED_QUERY_PARAMETERS = {
    "get /api/v1/projects": ("include_archived", "limit", "cursor"),
    "get /api/v1/tasks": (
        "date",
        "project_ids",
        "priorities",
        "status",
        "recurrence",
        "q",
        "include_archived_projects",
        "limit",
        "cursor",
    ),
}

EXPECTED_SUCCESS_SCHEMAS = {
    "delete /api/v1/labels/{label_id}": {"204": None},
    "delete /api/v1/projects/{project_id}": {"204": None},
    "delete /api/v1/task-series/{series_id}": {"204": None},
    "delete /api/v1/tasks/{task_id}": {"204": None},
    "get /api/v1/auth/me": {"200": "UserResponse"},
    "get /api/v1/color-palette": {"200": "ColorPaletteResponse"},
    "get /api/v1/labels": {"200": "LabelListResponse"},
    "get /api/v1/projects": {"200": "ProjectListResponse"},
    "get /api/v1/projects/{project_id}": {"200": "ProjectResponse"},
    "get /api/v1/task-series": {"200": "SeriesListResponse"},
    "get /api/v1/task-series/{series_id}": {"200": "SeriesResponse"},
    "get /api/v1/tasks": {"200": "TaskListResponse"},
    "get /api/v1/tasks/today/progress": {"200": "TodayProgressResponse"},
    "get /api/v1/tasks/{task_id}": {"200": "TaskResponse"},
    "get /health/live": {"200": "Response Health-Live"},
    "get /health/ready": {"200": "HealthResponse"},
    "patch /api/v1/auth/me/timezone": {"200": "UserResponse"},
    "patch /api/v1/labels/{label_id}": {"200": "LabelResponse"},
    "patch /api/v1/projects/{project_id}": {"200": "ProjectResponse"},
    "patch /api/v1/task-series/{series_id}": {"200": "SeriesResponse"},
    "patch /api/v1/tasks/{task_id}": {"200": "TaskResponse"},
    "post /api/v1/auth/logout": {"204": None},
    "post /api/v1/auth/refresh": {"200": "TokenResponse"},
    "post /api/v1/auth/telegram": {"200": "AuthResponse"},
    "post /api/v1/labels": {"201": "LabelResponse"},
    "post /api/v1/projects": {"201": "ProjectResponse"},
    "post /api/v1/projects/{project_id}/archive": {"200": "ProjectResponse"},
    "post /api/v1/projects/{project_id}/unarchive": {"200": "ProjectResponse"},
    "post /api/v1/task-series": {"201": "SeriesResponse"},
    "post /api/v1/tasks": {"201": "TaskResponse"},
    "post /api/v1/tasks/{task_id}/complete": {"200": "TaskResponse"},
    "post /api/v1/tasks/{task_id}/uncomplete": {"200": "TaskResponse"},
}


def _operation(spec: dict[str, Any], key: str) -> dict[str, Any]:
    """Return the OpenAPI operation for a ``method path`` key."""
    method, path = key.split(" ", 1)
    operation: dict[str, Any] = spec["paths"][path][method]
    return operation


def _response_schema_name(response: dict[str, Any]) -> str | None:
    """Return the referenced or inline response schema name, or ``None``."""
    content = response.get("content") or {}
    for media in content.values():
        schema = media.get("schema")
        if not isinstance(schema, dict):
            continue
        reference = schema.get("$ref")
        if isinstance(reference, str):
            return reference.rsplit("/", 1)[-1]
        title = schema.get("title")
        if isinstance(title, str):
            return title
    return None


def _success_response_schemas(operation: dict[str, Any]) -> dict[str, str | None]:
    """Map documented 2xx statuses to their response schema names."""
    return {
        status: _response_schema_name(response)
        for status, response in operation["responses"].items()
        if status.startswith("2")
    }


def _query_parameter_names(operation: dict[str, Any]) -> tuple[str, ...]:
    """Return query parameter names in their documented order."""
    return tuple(
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    )


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
        operation = _operation(spec, key)
        documented = {status for status in operation["responses"] if status.startswith(("4", "5"))}
        assert documented == statuses
        for status in statuses:
            assert _response_schema_name(operation["responses"][status]) == "ErrorResponse"


def test_error_status_matrix_covers_every_operation() -> None:
    """Every frozen operation documents exactly one error-status set."""
    assert set(EXPECTED_ERROR_STATUSES) == set(EXPECTED_OPERATION_IDS)


def test_openapi_query_parameters_and_success_schemas_are_frozen() -> None:
    """Query parameters and success response schema names stay stable."""
    app = create_app(Settings(_env_file=None, app_env="local", log_json=False))
    spec = app.openapi()

    assert set(EXPECTED_QUERY_PARAMETERS) <= set(EXPECTED_OPERATION_IDS)
    actual_query_parameters = {
        key: _query_parameter_names(_operation(spec, key)) for key in EXPECTED_OPERATION_IDS
    }
    expected_query_parameters = {
        key: EXPECTED_QUERY_PARAMETERS.get(key, ()) for key in EXPECTED_OPERATION_IDS
    }
    assert actual_query_parameters == expected_query_parameters

    actual_success_schemas = {
        key: _success_response_schemas(_operation(spec, key)) for key in EXPECTED_OPERATION_IDS
    }
    assert actual_success_schemas == EXPECTED_SUCCESS_SCHEMAS
