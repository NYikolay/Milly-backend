"""Mutable persistence-shaped domain entities without ORM dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from uuid import UUID


@dataclass
class User:
    """Application user owning a Telegram profile and refresh sessions."""

    id: int = 0
    timezone: str = "UTC"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    telegram_profile: TelegramProfile | None = None


@dataclass
class TelegramProfile:
    """Telegram identity linked one-to-one with an application user."""

    user_id: int
    telegram_id: int
    first_name: str
    id: int = 0
    is_bot: bool | None = None
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool = False
    added_to_attachment_menu: bool | None = None
    allows_write_to_pm: bool | None = None
    photo_url: str | None = None


@dataclass
class AuthSession:
    """Refresh-token session used for rotation and reuse detection."""

    user_id: int
    family_id: UUID
    refresh_token_hash: str
    expires_at: datetime
    id: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None


@dataclass
class ColorPalette:
    """Global color palette entry referenced by projects and labels."""

    slug: str
    name: str
    hex: str
    sort_order: int
    id: int = 0
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Label:
    """User-owned label assignable to tasks and recurring series."""

    user_id: int
    name: str
    color_id: int
    id: int = 0
    deleted_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Project:
    """User-owned project grouping tasks and recurring series."""

    user_id: int
    name: str
    color_id: int
    id: int = 0
    description: str | None = None
    icon: str | None = None
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    deletion_batch_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class TaskSeries:
    """Recurring daily or periodic task template owned by a user."""

    user_id: int
    title: str
    time_of_day: time
    timezone: str
    starts_on: date
    id: int = 0
    project_id: int | None = None
    description: str | None = None
    priority: str = "neither"
    frequency: str = "daily"
    interval_days: int = 1
    ends_on: date | None = None
    generated_through: datetime | None = None
    reminders_enabled: bool = False
    deleted_at: datetime | None = None
    deletion_batch_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Task:
    """One-off or series-generated task owned by a user."""

    user_id: int
    title: str
    due_at: datetime
    id: int = 0
    series_id: int | None = None
    project_id: int | None = None
    description: str | None = None
    priority: str = "neither"
    completed_at: datetime | None = None
    reminders_enabled: bool = False
    deleted_at: datetime | None = None
    deletion_batch_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class TaskReminder:
    """Reminder rule attached to a task or a recurring series."""

    user_id: int
    kind: str
    id: int = 0
    task_id: int | None = None
    series_id: int | None = None
    enabled: bool = True
    offset_minutes: int | None = None
    interval_days: int | None = None
    time_of_day: time | None = None
    timezone: str | None = None
    next_run_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ReminderDelivery:
    """Idempotent delivery attempt for a scheduled task reminder."""

    reminder_id: int
    user_id: int
    scheduled_for: datetime
    id: int = 0
    status: str = "pending"
    attempts: int = 0
    locked_at: datetime | None = None
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    telegram_message_id: int | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class TaskLabel:
    """Assignment of a user label to a task."""

    task_id: int
    label_id: int
    user_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class SeriesLabel:
    """Assignment of a user label to a recurring task series."""

    series_id: int
    label_id: int
    user_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
