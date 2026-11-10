from __future__ import annotations

import ast
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from backend.api.mappers.task_management import (
    color_response,
    label_response,
    project_response,
    reminder_response,
    series_response,
    task_list_response,
    task_response,
)
from backend.api.pagination import encode_cursor, filter_fingerprint
from backend.api.queries.projects import (
    encode_project_cursor,
    parse_project_cursor,
    project_filter_fingerprint,
)
from backend.api.queries.tasks import (
    encode_task_cursor,
    parse_task_cursor,
    task_filter_fingerprint,
)
from backend.api.task_schemas import TaskFiltersQuery
from backend.core.errors import BadRequestError
from backend.domain.entities import (
    ColorPalette,
    Label,
    Project,
    Task,
    TaskReminder,
    TaskSeries,
)
from backend.domain.enums import Priority, ReminderKind, SeriesFrequency, TaskRecurrence
from backend.domain.task_management.models import (
    ProjectListCursor,
    ProjectPage,
    ProjectProgress,
    ProjectSummary,
    SeriesDetail,
    TaskDetail,
    TaskListCursor,
    TaskPage,
)

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
DUE_AT = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
NEXT_RUN_AT = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


def _color(color_id: int = 1) -> ColorPalette:
    """Build a domain palette color."""
    return ColorPalette(
        id=color_id,
        slug="blue",
        name="Blue",
        hex="#0000FF",
        sort_order=1,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _reminder(*, task_id: int | None = 1) -> TaskReminder:
    """Build a domain task or series reminder."""
    return TaskReminder(
        id=4,
        user_id=1,
        task_id=task_id,
        series_id=None if task_id is not None else 1,
        kind=ReminderKind.BEFORE_DUE.value,
        enabled=True,
        offset_minutes=30,
        interval_days=None,
        time_of_day=None,
        timezone="Europe/Moscow",
        next_run_at=NEXT_RUN_AT,
        created_at=NOW,
        updated_at=NOW,
    )


def _task_detail() -> TaskDetail:
    """Build a domain task detail with one label and one reminder."""
    task = Task(
        id=1,
        user_id=1,
        series_id=None,
        project_id=2,
        title="Write report",
        description="Description",
        priority=Priority.URGENT_IMPORTANT.value,
        due_at=DUE_AT,
        completed_at=None,
        reminders_enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )
    label = Label(
        id=3,
        user_id=1,
        name="Home",
        color_id=1,
        created_at=NOW,
        updated_at=NOW,
    )
    return TaskDetail(
        task=task,
        labels=[label],
        reminders=[_reminder()],
        project=None,
        recurrence=TaskRecurrence.PERIODIC,
    )


def _series_detail() -> SeriesDetail:
    """Build a domain series detail with one label and one reminder."""
    series = TaskSeries(
        id=5,
        user_id=1,
        project_id=2,
        title="Daily",
        description="Every day",
        priority=Priority.IMPORTANT_NOT_URGENT.value,
        frequency=SeriesFrequency.DAILY.value,
        interval_days=1,
        time_of_day=time(9, 0),
        timezone="Europe/Moscow",
        starts_on=date(2026, 11, 4),
        ends_on=date(2026, 12, 4),
        generated_through=NEXT_RUN_AT,
        reminders_enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )
    label = Label(
        id=3,
        user_id=1,
        name="Home",
        color_id=1,
        created_at=NOW,
        updated_at=NOW,
    )
    return SeriesDetail(series=series, labels=[label], reminders=[_reminder(task_id=None)])


def _project_summary() -> ProjectSummary:
    """Build a domain project summary with progress and color."""
    project = Project(
        id=2,
        user_id=1,
        name="Work",
        description="Plan",
        color_id=1,
        icon="briefcase",
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return ProjectSummary(
        project=project,
        progress=ProjectProgress(total=4, completed=1, remaining=3),
        color=_color(),
    )


def test_task_response_maps_domain_detail() -> None:
    """Task response mapping does not depend on ORM rows."""
    detail = _task_detail()

    response = task_response(detail)

    assert response.id == detail.task.id
    assert response.series_id == detail.task.series_id
    assert response.project_id == detail.task.project_id
    assert response.title == detail.task.title
    assert response.description == detail.task.description
    assert response.priority is Priority.URGENT_IMPORTANT
    assert response.recurrence is detail.recurrence
    assert response.due_at == detail.task.due_at
    assert response.completed_at is None
    assert response.reminders_enabled is True
    assert response.labels[0].id == 3
    assert response.labels[0].name == "Home"
    assert response.reminders[0].kind == "before_due"
    assert response.reminders[0].offset_minutes == 30
    assert response.reminders[0].next_run_at == NEXT_RUN_AT
    assert response.created_at == NOW
    assert response.updated_at == NOW


def test_task_list_response_maps_page_and_encoded_cursor() -> None:
    """Task-list mapping serializes every detail and carries the cursor token."""
    detail = _task_detail()
    page = TaskPage(
        items=[detail],
        next_cursor=TaskListCursor(due_at=detail.task.due_at, task_id=detail.task.id),
    )

    response = task_list_response(page, next_cursor="next-token")

    assert [item.id for item in response.items] == [1]
    assert response.next_cursor == "next-token"


def test_task_list_response_rejects_dropped_cursor() -> None:
    """Task-list mapping refuses to drop a domain next-page cursor."""
    detail = _task_detail()
    page = TaskPage(
        items=[detail],
        next_cursor=TaskListCursor(due_at=detail.task.due_at, task_id=detail.task.id),
    )

    with pytest.raises(AssertionError, match="next_cursor"):
        task_list_response(page)


def test_series_response_maps_domain_detail() -> None:
    """Series response mapping does not depend on ORM rows."""
    detail = _series_detail()

    response = series_response(detail)

    assert response.id == detail.series.id
    assert response.title == detail.series.title
    assert response.description == detail.series.description
    assert response.priority is Priority.IMPORTANT_NOT_URGENT
    assert response.frequency is SeriesFrequency.DAILY
    assert response.interval_days == 1
    assert response.time_of_day == time(9, 0)
    assert response.timezone == "Europe/Moscow"
    assert response.starts_on == date(2026, 11, 4)
    assert response.ends_on == date(2026, 12, 4)
    assert response.generated_through == NEXT_RUN_AT
    assert response.reminders_enabled is True
    assert response.labels[0].id == 3
    assert response.reminders[0].kind == "before_due"
    assert response.created_at == NOW
    assert response.updated_at == NOW


def test_project_response_maps_domain_summary() -> None:
    """Project response mapping uses the domain summary and palette color."""
    summary = _project_summary()

    response = project_response(summary)

    assert response.id == summary.project.id
    assert response.name == summary.project.name
    assert response.description == summary.project.description
    assert response.icon == summary.project.icon
    assert response.color.id == summary.color.id
    assert response.color.slug == summary.color.slug
    assert response.color.name == summary.color.name
    assert response.color.hex == summary.color.hex
    assert response.progress.total == 4
    assert response.progress.completed == 1
    assert response.progress.remaining == 3
    assert response.archived_at is None
    assert response.created_at == NOW
    assert response.updated_at == NOW


def test_color_response_maps_domain_palette() -> None:
    """Color response mapping exposes only the public palette fields."""
    color = _color(color_id=7)

    response = color_response(color)

    assert response.id == 7
    assert response.slug == "blue"
    assert response.name == "Blue"
    assert response.hex == "#0000FF"


def test_label_response_maps_domain_label_and_color() -> None:
    """Label response mapping embeds the resolved palette color."""
    label = Label(
        id=3,
        user_id=1,
        name="Home",
        color_id=7,
        created_at=NOW,
        updated_at=NOW,
    )

    response = label_response(label, _color(color_id=7))

    assert response.id == 3
    assert response.name == "Home"
    assert response.color.id == 7
    assert response.color.hex == "#0000FF"
    assert response.created_at == NOW
    assert response.updated_at == NOW


def test_reminder_response_maps_domain_reminder() -> None:
    """Reminder response mapping preserves configuration and next run."""
    reminder = _reminder()

    response = reminder_response(reminder)

    assert response.id == 4
    assert response.kind == "before_due"
    assert response.enabled is True
    assert response.offset_minutes == 30
    assert response.interval_days is None
    assert response.time_of_day is None
    assert response.timezone == "Europe/Moscow"
    assert response.next_run_at == NEXT_RUN_AT


def test_task_cursor_round_trip_preserves_format() -> None:
    """Task cursors keep the existing token format and filter fingerprint."""
    filters = TaskFiltersQuery()
    fingerprint = task_filter_fingerprint(filters)
    expected_fingerprint = filter_fingerprint(
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
    cursor = TaskListCursor(due_at=DUE_AT, task_id=42)

    token = encode_task_cursor(cursor, user_id=7, filter_fingerprint=fingerprint)

    assert fingerprint == expected_fingerprint
    assert token == encode_cursor(
        user_id=7,
        filter_fingerprint=expected_fingerprint,
        order_key=[DUE_AT.isoformat(), 42],
    )
    assert parse_task_cursor(token, user_id=7, filter_fingerprint=fingerprint) == cursor


def test_project_cursor_round_trip_preserves_format() -> None:
    """Project cursors keep the existing token format and filter fingerprint."""
    fingerprint = project_filter_fingerprint(include_archived=True)
    expected_fingerprint = filter_fingerprint({"include_archived": True})
    cursor = ProjectListCursor(is_archived=True, name_key="work", project_id=42)

    token = encode_project_cursor(cursor, user_id=7, filter_fingerprint=fingerprint)

    assert fingerprint == expected_fingerprint
    assert token == encode_cursor(
        user_id=7,
        filter_fingerprint=expected_fingerprint,
        order_key=[1, "work", 42],
    )
    assert parse_project_cursor(token, user_id=7, filter_fingerprint=fingerprint) == cursor


@pytest.mark.parametrize("user_id,fingerprint", [(8, "mismatch"), (7, "mismatch")])
def test_cursor_parse_rejects_foreign_binding(user_id: int, fingerprint: str) -> None:
    """Reject cursors issued for another user or another filter set."""
    token = encode_task_cursor(
        TaskListCursor(due_at=DUE_AT, task_id=42),
        user_id=7,
        filter_fingerprint=task_filter_fingerprint(TaskFiltersQuery()),
    )

    with pytest.raises(BadRequestError):
        parse_task_cursor(token, user_id=user_id, filter_fingerprint=fingerprint)


def test_cursor_parse_rejects_malformed_order_key() -> None:
    """Reject a cursor whose ordering key has the wrong shape."""
    token = encode_cursor(user_id=7, filter_fingerprint="fp", order_key=[1, 2])

    with pytest.raises(BadRequestError):
        parse_task_cursor(token, user_id=7, filter_fingerprint="fp")


def test_api_mapper_modules_do_not_import_orm() -> None:
    """API schemas, mappers, queries, and thin routes never import backend.models."""
    sources = [
        Path("src/backend/api/task_schemas.py"),
        Path("src/backend/api/mappers/task_management.py"),
        Path("src/backend/api/queries/tasks.py"),
        Path("src/backend/api/queries/projects.py"),
        Path("src/backend/api/routes/tasks.py"),
        Path("src/backend/api/routes/projects.py"),
        Path("src/backend/api/routes/task_series.py"),
        Path("src/backend/api/routes/color_palette.py"),
    ]
    for path in sources:
        tree = ast.parse(path.read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(name.startswith("backend.models") for name in imported), path


def test_project_page_mapper_inputs_do_not_require_orm() -> None:
    """Project pages are composed from domain summaries only."""
    page = ProjectPage(items=[_project_summary()], next_cursor=None)

    assert [project_response(item).id for item in page.items] == [2]
