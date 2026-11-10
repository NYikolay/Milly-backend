"""Round-trip tests for SQLAlchemy ORM to domain entity mappers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from backend.domain.entities import AuthSession as AuthSessionEntity
from backend.domain.entities import ColorPalette as ColorPaletteEntity
from backend.domain.entities import Label as LabelEntity
from backend.domain.entities import Project as ProjectEntity
from backend.domain.entities import ReminderDelivery as ReminderDeliveryEntity
from backend.domain.entities import SeriesLabel as SeriesLabelEntity
from backend.domain.entities import Task as TaskEntity
from backend.domain.entities import TaskLabel as TaskLabelEntity
from backend.domain.entities import TaskReminder as TaskReminderEntity
from backend.domain.entities import TaskSeries as TaskSeriesEntity
from backend.domain.entities import TelegramProfile as TelegramProfileEntity
from backend.domain.entities import User as UserEntity
from backend.domain.enums import DeliveryStatus, Priority, ReminderKind, SeriesFrequency
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_auth_session,
    apply_color,
    apply_delivery,
    apply_label,
    apply_profile,
    apply_project,
    apply_reminder,
    apply_series,
    apply_task,
    apply_user,
    auth_session_to_entity,
    color_to_entity,
    delivery_to_entity,
    label_to_entity,
    profile_to_entity,
    project_to_entity,
    reminder_to_entity,
    series_label_to_entity,
    series_to_entity,
    task_label_to_entity,
    task_to_entity,
    user_to_entity,
)
from backend.models.auth_session import AuthSession as AuthSessionRow
from backend.models.color_palette import ColorPalette as ColorPaletteRow
from backend.models.label import Label as LabelRow
from backend.models.project import Project as ProjectRow
from backend.models.reminder_delivery import ReminderDelivery as ReminderDeliveryRow
from backend.models.series_label import SeriesLabel as SeriesLabelRow
from backend.models.task import Task as TaskRow
from backend.models.task_label import TaskLabel as TaskLabelRow
from backend.models.task_reminder import TaskReminder as TaskReminderRow
from backend.models.task_series import TaskSeries as TaskSeriesRow
from backend.models.telegram_profile import TelegramProfile as TelegramProfileRow
from backend.models.user import User as UserRow

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
LATER = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
BATCH_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_BATCH_ID = UUID("22222222-2222-2222-2222-222222222222")


def _assert_round_trip(
    entity: Any,
    row: Any,
    apply: Callable[[Any, Any], None],
    changes: dict[str, Any],
    *,
    persisted: frozenset[str] | None = None,
) -> None:
    """
    Verify that mapping copies every persisted field in both directions.

    Purpose:
        Keep mapper tests exhaustive: ``changes`` must cover every persisted
        domain field, so a forgotten assignment fails the test.

    Args:
        entity: Domain entity produced by the ``*_to_entity`` mapper.
        row: ORM row used for the round trip.
        apply: ``apply_*`` mapper that writes the entity back into the row.
        changes: Distinct value for every persisted field.
        persisted: Optional field allow-list for entities with relationships.
    """
    names = {item.name for item in fields(entity)}
    if persisted is not None:
        names &= persisted
    assert names == set(changes)
    for name in names:
        assert getattr(entity, name) == getattr(row, name)

    updated = replace(entity, **changes)
    apply(updated, row)

    for name in names:
        assert getattr(row, name) == changes[name]


def test_user_mapper_round_trip_preserves_persisted_fields() -> None:
    """User mapping copies persisted columns and never touches the profile relationship."""
    profile = TelegramProfileRow(
        id=9,
        user_id=1,
        telegram_id=99,
        first_name="Ada",
    )
    row = UserRow(id=1, timezone="Europe/Moscow", created_at=NOW, updated_at=NOW)
    row.telegram_profile = profile

    entity = user_to_entity(row)
    assert isinstance(entity, UserEntity)
    _assert_round_trip(
        entity,
        row,
        apply_user,
        {
            "id": 2,
            "timezone": "Asia/Tokyo",
            "created_at": LATER,
            "updated_at": LATER,
        },
        persisted=frozenset({"id", "timezone", "created_at", "updated_at"}),
    )
    assert row.telegram_profile is profile


def test_profile_mapper_round_trip_preserves_persisted_fields() -> None:
    """Telegram profile mapping preserves every persisted column."""
    row = TelegramProfileRow(
        id=1,
        user_id=7,
        telegram_id=123,
        is_bot=False,
        first_name="Ada",
        last_name="Lovelace",
        username="ada",
        language_code="en",
        is_premium=True,
        added_to_attachment_menu=False,
        allows_write_to_pm=True,
        photo_url="https://example.test/ada.png",
    )

    entity = profile_to_entity(row)
    assert isinstance(entity, TelegramProfileEntity)
    _assert_round_trip(
        entity,
        row,
        apply_profile,
        {
            "id": 2,
            "user_id": 8,
            "telegram_id": 456,
            "is_bot": True,
            "first_name": "Grace",
            "last_name": "Hopper",
            "username": "grace",
            "language_code": "de",
            "is_premium": False,
            "added_to_attachment_menu": True,
            "allows_write_to_pm": False,
            "photo_url": None,
        },
    )


def test_auth_session_mapper_round_trip_preserves_persisted_fields() -> None:
    """Auth session mapping preserves identifiers, hashes, and expiry state."""
    family_id = UUID("33333333-3333-3333-3333-333333333333")
    row = AuthSessionRow(
        id=1,
        user_id=7,
        family_id=family_id,
        refresh_token_hash="a" * 64,
        created_at=NOW,
        expires_at=NOW,
        revoked_at=None,
    )

    entity = auth_session_to_entity(row)
    assert isinstance(entity, AuthSessionEntity)
    _assert_round_trip(
        entity,
        row,
        apply_auth_session,
        {
            "id": 2,
            "user_id": 8,
            "family_id": UUID("44444444-4444-4444-4444-444444444444"),
            "refresh_token_hash": "b" * 64,
            "created_at": LATER,
            "expires_at": LATER,
            "revoked_at": LATER,
        },
    )


def test_color_mapper_round_trip_preserves_persisted_fields() -> None:
    """Color palette mapping preserves slug, styling, and ordering fields."""
    row = ColorPaletteRow(
        id=1,
        slug="ocean",
        name="Ocean",
        hex="#123456",
        sort_order=3,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = color_to_entity(row)
    assert isinstance(entity, ColorPaletteEntity)
    _assert_round_trip(
        entity,
        row,
        apply_color,
        {
            "id": 2,
            "slug": "forest",
            "name": "Forest",
            "hex": "#654321",
            "sort_order": 4,
            "is_active": False,
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_label_mapper_round_trip_preserves_persisted_fields() -> None:
    """Label mapping preserves ownership, color, and soft-deletion fields."""
    row = LabelRow(
        id=1,
        user_id=7,
        name="Home",
        color_id=2,
        deleted_at=None,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = label_to_entity(row)
    assert isinstance(entity, LabelEntity)
    _assert_round_trip(
        entity,
        row,
        apply_label,
        {
            "id": 2,
            "user_id": 8,
            "name": "Work",
            "color_id": 3,
            "deleted_at": LATER,
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_project_mapper_round_trip_preserves_persisted_fields() -> None:
    """Project mapping preserves archival, deletion, and display fields."""
    row = ProjectRow(
        id=1,
        user_id=7,
        name="Home",
        description="Chores",
        color_id=2,
        icon="house",
        archived_at=None,
        deleted_at=None,
        deletion_batch_id=None,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = project_to_entity(row)
    assert isinstance(entity, ProjectEntity)
    _assert_round_trip(
        entity,
        row,
        apply_project,
        {
            "id": 2,
            "user_id": 8,
            "name": "Work",
            "description": "Tasks",
            "color_id": 3,
            "icon": "briefcase",
            "archived_at": LATER,
            "deleted_at": LATER,
            "deletion_batch_id": OTHER_BATCH_ID,
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_series_mapper_round_trip_preserves_persisted_fields() -> None:
    """Series mapping preserves recurrence, schedule, and reminder fields."""
    row = TaskSeriesRow(
        id=1,
        user_id=7,
        project_id=2,
        title="Daily review",
        description="Review the day",
        priority=Priority.URGENT_IMPORTANT.value,
        frequency=SeriesFrequency.DAILY.value,
        interval_days=1,
        time_of_day=time(9, 30),
        timezone="Europe/Moscow",
        starts_on=date(2026, 1, 1),
        ends_on=None,
        generated_through=None,
        reminders_enabled=True,
        deleted_at=None,
        deletion_batch_id=None,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = series_to_entity(row)
    assert isinstance(entity, TaskSeriesEntity)
    _assert_round_trip(
        entity,
        row,
        apply_series,
        {
            "id": 2,
            "user_id": 8,
            "project_id": 3,
            "title": "Weekly review",
            "description": None,
            "priority": Priority.NEITHER.value,
            "frequency": SeriesFrequency.PERIODIC.value,
            "interval_days": 7,
            "time_of_day": time(18, 0),
            "timezone": "Asia/Tokyo",
            "starts_on": date(2026, 2, 1),
            "ends_on": date(2026, 3, 1),
            "generated_through": LATER,
            "reminders_enabled": False,
            "deleted_at": LATER,
            "deletion_batch_id": BATCH_ID,
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_task_mapper_round_trip_preserves_persisted_fields() -> None:
    """Task mapping preserves ownership, scheduling, and completion fields."""
    row = TaskRow(
        id=1,
        user_id=7,
        series_id=3,
        project_id=2,
        title="Write",
        description="Body",
        priority=Priority.URGENT_IMPORTANT.value,
        due_at=NOW,
        completed_at=None,
        reminders_enabled=True,
        deleted_at=None,
        deletion_batch_id=None,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = task_to_entity(row)
    assert isinstance(entity, TaskEntity)
    assert entity.id == 1
    assert entity.user_id == 7
    assert entity.series_id == 3
    assert entity.priority == "urgent_important"
    _assert_round_trip(
        entity,
        row,
        apply_task,
        {
            "id": 2,
            "user_id": 8,
            "series_id": 4,
            "project_id": 5,
            "title": "Updated",
            "description": None,
            "priority": Priority.NEITHER.value,
            "due_at": LATER,
            "completed_at": LATER,
            "reminders_enabled": False,
            "deleted_at": LATER,
            "deletion_batch_id": BATCH_ID,
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_reminder_mapper_round_trip_preserves_persisted_fields() -> None:
    """Reminder mapping preserves ownership, schedule, and delivery fields."""
    row = TaskReminderRow(
        id=1,
        user_id=7,
        task_id=2,
        series_id=None,
        kind=ReminderKind.BEFORE_DUE.value,
        enabled=True,
        offset_minutes=15,
        interval_days=None,
        time_of_day=None,
        timezone="Europe/Moscow",
        next_run_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = reminder_to_entity(row)
    assert isinstance(entity, TaskReminderEntity)
    _assert_round_trip(
        entity,
        row,
        apply_reminder,
        {
            "id": 2,
            "user_id": 8,
            "task_id": None,
            "series_id": 3,
            "kind": ReminderKind.SCHEDULED.value,
            "enabled": False,
            "offset_minutes": None,
            "interval_days": 2,
            "time_of_day": time(8, 15),
            "timezone": "Asia/Tokyo",
            "next_run_at": LATER,
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_delivery_mapper_round_trip_preserves_persisted_fields() -> None:
    """Delivery mapping preserves retry, lock, and Telegram result fields."""
    row = ReminderDeliveryRow(
        id=1,
        reminder_id=2,
        user_id=7,
        scheduled_for=NOW,
        status=DeliveryStatus.PENDING.value,
        attempts=0,
        locked_at=None,
        next_attempt_at=None,
        sent_at=None,
        telegram_message_id=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )

    entity = delivery_to_entity(row)
    assert isinstance(entity, ReminderDeliveryEntity)
    _assert_round_trip(
        entity,
        row,
        apply_delivery,
        {
            "id": 2,
            "reminder_id": 3,
            "user_id": 8,
            "scheduled_for": LATER,
            "status": DeliveryStatus.SENT.value,
            "attempts": 1,
            "locked_at": LATER,
            "next_attempt_at": LATER,
            "sent_at": LATER,
            "telegram_message_id": 55,
            "last_error": "delivery failed",
            "created_at": LATER,
            "updated_at": LATER,
        },
    )


def test_task_label_mapper_preserves_columns() -> None:
    """Task-label assignment mapping preserves the composite key and audit fields."""
    row = TaskLabelRow(task_id=1, label_id=2, user_id=7, created_at=NOW)

    entity = task_label_to_entity(row)

    assert isinstance(entity, TaskLabelEntity)
    assert entity.task_id == 1
    assert entity.label_id == 2
    assert entity.user_id == 7
    assert entity.created_at == NOW


def test_series_label_mapper_preserves_columns() -> None:
    """Series-label assignment mapping preserves the composite key and audit fields."""
    row = SeriesLabelRow(series_id=1, label_id=2, user_id=7, created_at=NOW)

    entity = series_label_to_entity(row)

    assert isinstance(entity, SeriesLabelEntity)
    assert entity.series_id == 1
    assert entity.label_id == 2
    assert entity.user_id == 7
    assert entity.created_at == NOW
