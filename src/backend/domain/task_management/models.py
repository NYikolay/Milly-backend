"""Frozen task-management DTOs shared by application and transport layers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from backend.domain.entities import (
    ColorPalette,
    Label,
    Project,
    Task,
    TaskReminder,
    TaskSeries,
)
from backend.domain.enums import Priority, ReminderKind, TaskRecurrence


class UnsetType:
    """Sentinel type for optional series update fields that were not provided."""


UNSET = UnsetType()


@dataclass(frozen=True)
class TaskFilters:
    """Filters applied when listing tasks."""

    date: date | None = None
    project_ids: tuple[int, ...] = ()
    priorities: tuple[Priority, ...] = ()
    status: Literal["all", "open", "completed", "overdue"] = "all"
    recurrence: Literal["all", "regular", "periodic", "one_off"] = "all"
    query: str | None = None
    include_archived_projects: bool = False


@dataclass(frozen=True)
class TaskDetail:
    """A task with its labels, reminders, owning project, and recurrence."""

    task: Task
    labels: Sequence[Label]
    reminders: Sequence[TaskReminder]
    project: Project | None
    recurrence: TaskRecurrence = TaskRecurrence.ONE_OFF


@dataclass(frozen=True)
class TaskListCursor:
    """Ordering key of the last task on a page."""

    due_at: datetime
    task_id: int


@dataclass(frozen=True)
class TaskPage:
    """One page of task details with a cursor for the next page."""

    items: Sequence[TaskDetail]
    next_cursor: TaskListCursor | None


@dataclass(frozen=True)
class ProjectProgress:
    """Task completion counters for one project."""

    total: int
    completed: int
    remaining: int


@dataclass(frozen=True)
class ProjectSummary:
    """A project with its progress and resolved palette color."""

    project: Project
    progress: ProjectProgress
    color: ColorPalette


@dataclass(frozen=True)
class ProjectListCursor:
    """Ordering key of the last project on a page."""

    is_archived: bool
    name_key: str
    project_id: int


@dataclass(frozen=True)
class ProjectPage:
    """One page of project summaries with a cursor for the next page."""

    items: Sequence[ProjectSummary]
    next_cursor: ProjectListCursor | None


@dataclass(frozen=True)
class SeriesDetail:
    """A recurring series with its labels and template reminders."""

    series: TaskSeries
    labels: Sequence[Label]
    reminders: Sequence[TaskReminder]


@dataclass(frozen=True)
class ReminderInput:
    """Validated reminder rule supplied when creating or updating tasks."""

    kind: ReminderKind
    enabled: bool = True
    offset_minutes: int | None = None
    interval_days: int | None = None
    time_of_day: time | None = None


@dataclass(frozen=True)
class ReminderContent:
    """Resolved task or series data used to render a reminder message."""

    title: str
    description: str | None
    priority: str
    due_at: datetime | None
    project_name: str | None


@dataclass(frozen=True)
class ProjectReminderContext:
    """Reminder rule with the project data needed to recompute its slot."""

    reminder: TaskReminder
    task_due_at: datetime | None
    series_timezone: str | None
    is_series: bool
