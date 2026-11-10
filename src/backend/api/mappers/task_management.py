"""Mappers from task-management domain DTOs to API response models."""

from __future__ import annotations

from backend.api.task_schemas import (
    ColorResponse,
    LabelResponse,
    ProjectProgressResponse,
    ProjectResponse,
    ReminderResponse,
    SeriesResponse,
    TaskLabelResponse,
    TaskListResponse,
    TaskResponse,
)
from backend.domain.entities import ColorPalette, Label, TaskReminder
from backend.domain.enums import Priority, ReminderKind, SeriesFrequency
from backend.domain.task_management.models import (
    ProjectSummary,
    SeriesDetail,
    TaskDetail,
    TaskPage,
)


def reminder_response(reminder: TaskReminder) -> ReminderResponse:
    """
    Map a domain reminder to its API response.

    Purpose:
        Serialize reminder configuration together with the next computed slot
        for task and series responses without depending on ORM rows.

    Args:
        reminder: Domain task or series reminder.

    Returns:
        ReminderResponse: Reminder fields exposed to clients.
    """
    return ReminderResponse(
        id=reminder.id,
        kind=ReminderKind(reminder.kind),
        enabled=reminder.enabled,
        offset_minutes=reminder.offset_minutes,
        interval_days=reminder.interval_days,
        time_of_day=reminder.time_of_day,
        timezone=reminder.timezone,
        next_run_at=reminder.next_run_at,
    )


def task_response(detail: TaskDetail) -> TaskResponse:
    """
    Map a domain task detail to its API response.

    Purpose:
        Serialize one task consistently across list and mutation routes.

    Args:
        detail: Task detail returned by the task service.

    Returns:
        TaskResponse: The task with labels, reminders, and recurrence mode.
    """
    task = detail.task
    return TaskResponse(
        id=task.id,
        series_id=task.series_id,
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        priority=Priority(task.priority),
        recurrence=detail.recurrence,
        due_at=task.due_at,
        completed_at=task.completed_at,
        reminders_enabled=task.reminders_enabled,
        labels=[TaskLabelResponse(id=label.id, name=label.name) for label in detail.labels],
        reminders=[reminder_response(reminder) for reminder in detail.reminders],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def task_list_response(page: TaskPage, *, next_cursor: str | None = None) -> TaskListResponse:
    """
    Map one domain task page to its API response.

    Purpose:
        Serialize every task on the page together with the transport cursor
        encoded by the query layer.

    Args:
        page: Task page returned by the task service.
        next_cursor: Opaque cursor for the next page, or ``None`` on the last
            page.

    Returns:
        TaskListResponse: Task responses and the next-page cursor.
    """
    return TaskListResponse(
        items=[task_response(detail) for detail in page.items],
        next_cursor=next_cursor,
    )


def series_response(detail: SeriesDetail) -> SeriesResponse:
    """
    Map a domain series detail to its API response.

    Purpose:
        Serialize one recurring series with its schedule, labels, and template
        reminders.

    Args:
        detail: Series detail returned by the series service.

    Returns:
        SeriesResponse: The series fields exposed to clients, including its
            frequency and interval between occurrences.
    """
    series = detail.series
    return SeriesResponse(
        id=series.id,
        title=series.title,
        description=series.description,
        priority=Priority(series.priority),
        frequency=SeriesFrequency(series.frequency),
        interval_days=series.interval_days,
        time_of_day=series.time_of_day,
        timezone=series.timezone,
        starts_on=series.starts_on,
        ends_on=series.ends_on,
        generated_through=series.generated_through,
        reminders_enabled=series.reminders_enabled,
        labels=[TaskLabelResponse(id=label.id, name=label.name) for label in detail.labels],
        reminders=[reminder_response(reminder) for reminder in detail.reminders],
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


def color_response(color: ColorPalette) -> ColorResponse:
    """
    Map a domain palette color to its API response.

    Purpose:
        Expose the public color fields used by palette, project, and label
        responses.

    Args:
        color: Domain palette color.

    Returns:
        ColorResponse: Palette fields exposed to clients.
    """
    return ColorResponse(
        id=color.id,
        slug=color.slug,
        name=color.name,
        hex=color.hex,
    )


def project_response(summary: ProjectSummary) -> ProjectResponse:
    """
    Map a domain project summary to its API response.

    Purpose:
        Serialize the project, its resolved palette color, and progress
        counters consistently across project routes.

    Args:
        summary: Project summary returned by the project service.

    Returns:
        ProjectResponse: Project fields with color and progress data.
    """
    return ProjectResponse(
        id=summary.project.id,
        name=summary.project.name,
        description=summary.project.description,
        icon=summary.project.icon,
        color=color_response(summary.color),
        progress=ProjectProgressResponse(
            total=summary.progress.total,
            completed=summary.progress.completed,
            remaining=summary.progress.remaining,
        ),
        archived_at=summary.project.archived_at,
        created_at=summary.project.created_at,
        updated_at=summary.project.updated_at,
    )


def label_response(label: Label, color: ColorPalette) -> LabelResponse:
    """
    Map a domain label and its resolved color to an API response.

    Purpose:
        Serialize a label with the palette color referenced by its response
        contract.

    Args:
        label: Domain label owned by the authenticated user.
        color: Resolved palette color of the label.

    Returns:
        LabelResponse: Label fields with its embedded color.
    """
    return LabelResponse(
        id=label.id,
        name=label.name,
        color=color_response(color),
        created_at=label.created_at,
        updated_at=label.updated_at,
    )
