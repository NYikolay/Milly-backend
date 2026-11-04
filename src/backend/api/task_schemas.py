from __future__ import annotations

from datetime import date as Date
from datetime import datetime, time
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models.task_reminder import TaskReminder
from backend.task_management.date_bounds import (
    require_supported_date,
    require_supported_datetime,
)
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.reminders import ReminderInput
from backend.task_management.tasks import TaskFilters


class ColorResponse(BaseModel):
    """Palette color exposed to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    hex: str


class ColorPaletteResponse(BaseModel):
    """Selectable palette colors in display order."""

    items: list[ColorResponse]


class ProjectCreate(BaseModel):
    """Fields accepted when creating a project."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    color_id: int | None = Field(default=None, gt=0)
    icon: str | None = Field(default=None, max_length=32)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        """
        Trim a project name and reject an empty result.

        Purpose:
            Apply the shared project name rule at the request boundary.

        Args:
            value: Candidate project name.

        Returns:
            str: The trimmed name.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name must not be empty")
        return normalized


class ProjectUpdate(BaseModel):
    """Optional fields accepted when updating a project."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    color_id: int | None = Field(default=None, gt=0)
    icon: str | None = Field(default=None, max_length=32)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        """
        Trim an optional project name and reject an empty result.

        Purpose:
            Apply the shared project name rule while preserving omitted fields.

        Args:
            value: Candidate project name, or ``None``.

        Returns:
            str | None: The trimmed name or ``None``.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name must not be empty")
        return normalized


class ProjectProgressResponse(BaseModel):
    """Task completion counters for one project."""

    total: int
    completed: int
    remaining: int


class ProjectResponse(BaseModel):
    """Project with its palette color and progress counters."""

    id: int
    name: str
    description: str | None
    icon: str | None
    color: ColorResponse
    progress: ProjectProgressResponse
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """Projects visible to the authenticated user with a next-page cursor."""

    items: list[ProjectResponse]
    next_cursor: str | None = None


class LabelCreate(BaseModel):
    """Fields accepted when creating a label."""

    name: str = Field(min_length=1, max_length=60)
    color_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        """
        Trim a label name and reject an empty result.

        Purpose:
            Apply the shared label name rule at the request boundary.

        Args:
            value: Candidate label name.

        Returns:
            str: The trimmed name.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("Label name must not be empty")
        return normalized


class LabelUpdate(BaseModel):
    """Optional fields accepted when updating a label."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    color_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        """
        Trim an optional label name and reject an empty result.

        Purpose:
            Apply the shared label name rule while preserving omitted fields.

        Args:
            value: Candidate label name, or ``None``.

        Returns:
            str | None: The trimmed name or ``None``.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Label name must not be empty")
        return normalized


class LabelResponse(BaseModel):
    """Label with its resolved palette color."""

    id: int
    name: str
    color: ColorResponse
    created_at: datetime
    updated_at: datetime


class LabelListResponse(BaseModel):
    """Labels owned by the authenticated user."""

    items: list[LabelResponse]


def _require_aware(value: datetime | None) -> datetime | None:
    """
    Require an optional due instant to include a timezone.

    Purpose:
        Keep API due times as unambiguous absolute instants before they reach
        the task service.

    Args:
        value: Candidate due instant, or ``None``.

    Returns:
        datetime | None: The unchanged aware instant or ``None``.

    Raises:
        ValueError: If the instant is naive.
    """
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("Due time must include a timezone")
    return value


def _require_naive_time(value: time | None) -> time | None:
    """
    Require an optional wall-clock time to be timezone-naive.

    Purpose:
        Keep ``time_of_day`` a local series value so the service can resolve it
        through the series timezone instead of receiving an ambiguous mixed
        value.

    Args:
        value: Candidate wall-clock time, or ``None``.

    Returns:
        time | None: The unchanged naive time or ``None``.

    Raises:
        ValueError: If the value carries timezone information.
    """
    if value is not None and (value.tzinfo is not None or value.utcoffset() is not None):
        raise ValueError("Time of day must not include a timezone")
    return value


def _require_iana_timezone(value: str | None) -> str | None:
    """
    Require an optional timezone name to resolve as an IANA zone.

    Purpose:
        Reject unknown zones at the request boundary so clients receive the
        standard validation envelope.

    Args:
        value: Candidate IANA timezone name, or ``None``.

    Returns:
        str | None: The unchanged valid name or ``None``.

    Raises:
        ValueError: If the name cannot be resolved by ``ZoneInfo``.
    """
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Invalid timezone") from error
    return value


def _require_unique_reminder_kinds(
    reminders: list[ReminderInputSchema] | None,
) -> list[ReminderInputSchema] | None:
    """
    Reject duplicate reminder kinds in a request selection.

    Purpose:
        Match the partial unique index that allows at most one reminder per
        kind on a task or series.

    Args:
        reminders: Reminder rules, or ``None`` when unchanged.

    Returns:
        list[ReminderInputSchema] | None: The unchanged rules or ``None``.

    Raises:
        ValueError: If two rules share the same kind.
    """
    if reminders is None:
        return None
    kinds = [reminder.kind for reminder in reminders]
    if len(kinds) != len(set(kinds)):
        raise ValueError("Duplicate reminder kind")
    return reminders


class ReminderInputSchema(BaseModel):
    """Reminder rule accepted when creating or updating tasks and series."""

    kind: Literal["before_due", "scheduled"]
    enabled: bool = True
    offset_minutes: int | None = Field(default=None, ge=0, le=525_600)
    interval_days: int | None = Field(default=None, ge=1, le=32_767)
    time_of_day: time | None = None

    @field_validator("time_of_day")
    @classmethod
    def _validate_time_of_day(cls, value: time | None) -> time | None:
        """
        Reject a timezone-aware reminder wall-clock time.

        Purpose:
            Keep reminder slots resolvable through the stored timezone.

        Args:
            value: Candidate wall-clock time, or ``None``.

        Returns:
            time | None: The unchanged naive time or ``None``.

        Raises:
            ValueError: If the value carries timezone information.
        """
        return _require_naive_time(value)

    @model_validator(mode="after")
    def _validate_rule(self) -> Self:
        """
        Enforce the reminder kind and parameter invariants.

        Purpose:
            Apply the same rule shape as the database checks and the reminder
            service before any write is attempted.

        Returns:
            Self: The validated reminder rule.

        Raises:
            ValueError: If required parameters are missing or incompatible
                fields are supplied for the kind.
        """
        if self.kind == "before_due":
            if self.offset_minutes is None:
                raise ValueError("before_due reminders require offset_minutes")
            if self.interval_days is not None or self.time_of_day is not None:
                raise ValueError("before_due reminders accept only offset_minutes")
        else:
            if self.interval_days is None:
                raise ValueError("scheduled reminders require interval_days")
            if self.time_of_day is None:
                raise ValueError("scheduled reminders require time_of_day")
            if self.offset_minutes is not None:
                raise ValueError("scheduled reminders do not accept offset_minutes")
        return self

    def to_input(self) -> ReminderInput:
        """
        Convert the request schema into a service reminder rule.

        Purpose:
            Keep the API and service layers decoupled while sharing one
            validated reminder shape.

        Returns:
            ReminderInput: The equivalent service-level reminder input.
        """
        return ReminderInput(
            kind=ReminderKind(self.kind),
            enabled=self.enabled,
            offset_minutes=self.offset_minutes,
            interval_days=self.interval_days,
            time_of_day=self.time_of_day,
        )


class ReminderResponse(BaseModel):
    """Reminder rule and its next computed delivery slot."""

    id: int
    kind: Literal["before_due", "scheduled"]
    enabled: bool
    offset_minutes: int | None
    interval_days: int | None
    time_of_day: time | None
    timezone: str | None
    next_run_at: datetime | None

    @classmethod
    def from_row(cls, reminder: TaskReminder) -> ReminderResponse:
        """
        Build a reminder response from a persisted row.

        Purpose:
            Serialize reminder configuration together with its next computed
            slot for task and series responses.

        Args:
            reminder: Persisted task or series reminder.

        Returns:
            ReminderResponse: Reminder fields exposed to clients.
        """
        return cls(
            id=reminder.id,
            kind=ReminderKind(reminder.kind),
            enabled=reminder.enabled,
            offset_minutes=reminder.offset_minutes,
            interval_days=reminder.interval_days,
            time_of_day=reminder.time_of_day,
            timezone=reminder.timezone,
            next_run_at=reminder.next_run_at,
        )


class TaskLabelResponse(BaseModel):
    """Label reference embedded in a task or series response."""

    id: int
    name: str


class TaskCreate(BaseModel):
    """Fields accepted when creating a one-off task."""

    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    priority: Priority = Priority.NEITHER
    due_at: datetime
    project_id: int | None = Field(default=None, gt=0)
    label_ids: list[int] = Field(default_factory=list)
    reminders_enabled: bool = False
    reminders: list[ReminderInputSchema] = Field(default_factory=list)

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: datetime) -> datetime:
        """
        Require an aware due instant inside the supported range.

        Purpose:
            Reject naive or out-of-range due times before the service
            normalizes and stores them.

        Args:
            value: Candidate due instant.

        Returns:
            datetime: The unchanged supported instant.

        Raises:
            ValueError: If the instant is naive or outside the inclusive UTC
                bounds.
        """
        _require_aware(value)
        require_supported_datetime(value)
        return value

    @field_validator("reminders")
    @classmethod
    def _validate_reminders(
        cls,
        value: list[ReminderInputSchema],
    ) -> list[ReminderInputSchema]:
        """
        Reject duplicate reminder kinds.

        Purpose:
            Keep the create selection compatible with the per-kind uniqueness
            rule.

        Args:
            value: Reminder rules from the request.

        Returns:
            list[ReminderInputSchema]: The unchanged unique rules.

        Raises:
            ValueError: If two rules share the same kind.
        """
        return _require_unique_reminder_kinds(value) or []


class TaskUpdate(BaseModel):
    """Optional fields accepted when updating a task."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    priority: Priority | None = None
    due_at: datetime | None = None
    project_id: int | None = Field(default=None, gt=0)
    label_ids: list[int] | None = None
    reminders_enabled: bool | None = None
    reminders: list[ReminderInputSchema] | None = None

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: datetime | None) -> datetime | None:
        """
        Require an optional aware due instant inside the supported range.

        Purpose:
            Reject naive or out-of-range due times while preserving omitted
            fields.

        Args:
            value: Candidate due instant, or ``None``.

        Returns:
            datetime | None: The unchanged supported instant or ``None``.

        Raises:
            ValueError: If the instant is naive or outside the inclusive UTC
                bounds.
        """
        aware = _require_aware(value)
        if aware is not None:
            require_supported_datetime(aware)
        return aware

    @field_validator("reminders")
    @classmethod
    def _validate_reminders(
        cls,
        value: list[ReminderInputSchema] | None,
    ) -> list[ReminderInputSchema] | None:
        """
        Reject duplicate reminder kinds while preserving omission.

        Purpose:
            Keep the update selection compatible with the per-kind uniqueness
            rule.

        Args:
            value: Reminder rules from the request, or ``None``.

        Returns:
            list[ReminderInputSchema] | None: The unchanged unique rules or
                ``None``.

        Raises:
            ValueError: If two rules share the same kind.
        """
        return _require_unique_reminder_kinds(value)


class TaskResponse(BaseModel):
    """Task detail returned by task endpoints."""

    id: int
    series_id: int | None
    project_id: int | None
    title: str
    description: str | None
    priority: Priority
    due_at: datetime
    completed_at: datetime | None
    reminders_enabled: bool
    labels: list[TaskLabelResponse]
    reminders: list[ReminderResponse]
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Filtered tasks visible to the authenticated user with a next-page cursor."""

    items: list[TaskResponse]
    next_cursor: str | None = None


class TaskFiltersQuery(BaseModel):
    """Query parameters accepted by the task list endpoint."""

    date: Date | None = None
    project_ids: list[int] = Field(default_factory=list)
    priorities: list[Priority] = Field(default_factory=list)
    status: Literal["all", "open", "completed", "overdue"] = "all"
    recurrence: Literal["all", "regular", "one_off"] = "all"
    q: str | None = None
    include_archived_projects: bool = False

    @field_validator("date")
    @classmethod
    def _validate_supported_date(cls, value: Date | None) -> Date | None:
        """
        Require the optional date filter to fall inside the supported range.

        Purpose:
            Reject dates whose local-day bounds could overflow schedule
            arithmetic before the service is called.

        Args:
            value: Candidate filter date, or ``None`` when unfiltered.

        Returns:
            Date | None: The unchanged supported date or ``None``.

        Raises:
            ValueError: If the date is outside the inclusive supported range.
        """
        if value is None:
            return None
        return require_supported_date(value)

    def to_filters(self) -> TaskFilters:
        """
        Convert query parameters into service filters.

        Purpose:
            Keep the transport query model separate from the service filter
            dataclass.

        Returns:
            TaskFilters: The equivalent service-level filters.
        """
        return TaskFilters(
            date=self.date,
            project_ids=tuple(self.project_ids),
            priorities=tuple(self.priorities),
            status=self.status,
            recurrence=self.recurrence,
            query=self.q,
            include_archived_projects=self.include_archived_projects,
        )


class SeriesCreate(BaseModel):
    """Fields accepted when creating a daily recurring series."""

    title: str = Field(min_length=1, max_length=120)
    description: str | None = None
    priority: Priority = Priority.NEITHER
    time_of_day: time
    timezone: str = Field(min_length=1, max_length=64)
    starts_on: Date
    ends_on: Date | None = None
    project_id: int | None = Field(default=None, gt=0)
    label_ids: list[int] = Field(default_factory=list)
    reminders_enabled: bool = False
    reminders: list[ReminderInputSchema] = Field(default_factory=list)

    @field_validator("time_of_day")
    @classmethod
    def _validate_time_of_day(cls, value: time) -> time:
        """
        Reject a timezone-aware series wall-clock time.

        Purpose:
            Keep ``time_of_day`` local to the series timezone before generation.

        Args:
            value: Candidate wall-clock time.

        Returns:
            time: The unchanged naive time.

        Raises:
            ValueError: If the value carries timezone information.
        """
        validated = _require_naive_time(value)
        if validated is None:
            raise ValueError("Time of day is required")
        return validated

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """
        Require a valid IANA timezone name.

        Purpose:
            Reject unknown series zones before the service computes local
            occurrence times.

        Args:
            value: Candidate IANA timezone name.

        Returns:
            str: The unchanged valid name.

        Raises:
            ValueError: If the name cannot be resolved by ``ZoneInfo``.
        """
        validated = _require_iana_timezone(value)
        if validated is None:
            raise ValueError("Invalid timezone")
        return validated

    @field_validator("starts_on", "ends_on")
    @classmethod
    def _validate_supported_date(cls, value: Date | None) -> Date | None:
        """
        Require series calendar dates to fall inside the supported range.

        Purpose:
            Reject starts and ends that would make generation arithmetic walk
            over an unsupported span.

        Args:
            value: Candidate start or end date, or ``None`` for an open-ended
                series.

        Returns:
            Date | None: The unchanged supported date or ``None``.

        Raises:
            ValueError: If the date is outside the inclusive supported range.
        """
        if value is None:
            return None
        return require_supported_date(value)

    @field_validator("reminders")
    @classmethod
    def _validate_reminders(
        cls,
        value: list[ReminderInputSchema],
    ) -> list[ReminderInputSchema]:
        """
        Reject duplicate reminder kinds.

        Purpose:
            Keep the create selection compatible with the per-kind uniqueness
            rule.

        Args:
            value: Reminder rules from the request.

        Returns:
            list[ReminderInputSchema]: The unchanged unique rules.

        Raises:
            ValueError: If two rules share the same kind.
        """
        return _require_unique_reminder_kinds(value) or []


class SeriesUpdate(BaseModel):
    """Optional fields accepted when updating a recurring series."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    priority: Priority | None = None
    time_of_day: time | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    starts_on: Date | None = None
    ends_on: Date | None = None
    project_id: int | None = Field(default=None, gt=0)
    label_ids: list[int] | None = None
    reminders_enabled: bool | None = None
    reminders: list[ReminderInputSchema] | None = None

    @field_validator("time_of_day")
    @classmethod
    def _validate_time_of_day(cls, value: time | None) -> time | None:
        """
        Reject a timezone-aware series wall-clock time while preserving omission.

        Purpose:
            Keep updated series times local to the configured timezone.

        Args:
            value: Candidate wall-clock time, or ``None``.

        Returns:
            time | None: The unchanged naive time or ``None``.

        Raises:
            ValueError: If the value carries timezone information.
        """
        return _require_naive_time(value)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        """
        Require an optional valid IANA timezone name.

        Purpose:
            Reject unknown series zones while preserving omitted fields.

        Args:
            value: Candidate IANA timezone name, or ``None``.

        Returns:
            str | None: The unchanged valid name or ``None``.

        Raises:
            ValueError: If the name cannot be resolved by ``ZoneInfo``.
        """
        return _require_iana_timezone(value)

    @field_validator("starts_on", "ends_on")
    @classmethod
    def _validate_supported_date(cls, value: Date | None) -> Date | None:
        """
        Require updated series dates to fall inside the supported range.

        Purpose:
            Reject schedule edits whose dates would be outside the shared
            generation bounds.

        Args:
            value: Candidate start or end date, or ``None`` when omitted or
                clearing the end date.

        Returns:
            Date | None: The unchanged supported date or ``None``.

        Raises:
            ValueError: If the date is outside the inclusive supported range.
        """
        if value is None:
            return None
        return require_supported_date(value)

    @field_validator("reminders")
    @classmethod
    def _validate_reminders(
        cls,
        value: list[ReminderInputSchema] | None,
    ) -> list[ReminderInputSchema] | None:
        """
        Reject duplicate reminder kinds while preserving omission.

        Purpose:
            Keep the update selection compatible with the per-kind uniqueness
            rule.

        Args:
            value: Reminder rules from the request, or ``None``.

        Returns:
            list[ReminderInputSchema] | None: The unchanged unique rules or
                ``None``.

        Raises:
            ValueError: If two rules share the same kind.
        """
        return _require_unique_reminder_kinds(value)


class SeriesResponse(BaseModel):
    """Recurring series detail returned by series endpoints."""

    id: int
    title: str
    description: str | None
    priority: Priority
    frequency: Literal["daily"]
    time_of_day: time
    timezone: str
    starts_on: Date
    ends_on: Date | None
    generated_through: datetime | None
    reminders_enabled: bool
    labels: list[TaskLabelResponse]
    reminders: list[ReminderResponse]
    created_at: datetime
    updated_at: datetime


class SeriesListResponse(BaseModel):
    """Recurring series owned by the authenticated user."""

    items: list[SeriesResponse]
