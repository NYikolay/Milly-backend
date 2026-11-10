"""Explicit mappings between SQLAlchemy rows and ORM-free domain entities."""

from __future__ import annotations

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


def user_to_entity(row: UserRow) -> UserEntity:
    """
    Map an ORM user row to a domain user.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``User`` row.

    Returns:
        UserEntity: Domain user populated from persisted columns.

    Notes:
        - ``telegram_profile`` is a relationship and is not mapped here.
    """
    return UserEntity(
        id=row.id,
        timezone=row.timezone,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_user(entity: UserEntity, row: UserRow) -> None:
    """
    Copy a domain user onto an ORM user row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain user with the values to persist.
        row: SQLAlchemy ``User`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.

    Notes:
        - Copies persisted columns only; the ``telegram_profile`` relationship
          is never touched.
    """
    row.id = entity.id
    row.timezone = entity.timezone
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def profile_to_entity(row: TelegramProfileRow) -> TelegramProfileEntity:
    """
    Map an ORM Telegram profile row to a domain profile.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``TelegramProfile`` row.

    Returns:
        TelegramProfileEntity: Domain profile populated from persisted columns.
    """
    return TelegramProfileEntity(
        id=row.id,
        user_id=row.user_id,
        telegram_id=row.telegram_id,
        is_bot=row.is_bot,
        first_name=row.first_name,
        last_name=row.last_name,
        username=row.username,
        language_code=row.language_code,
        is_premium=row.is_premium,
        added_to_attachment_menu=row.added_to_attachment_menu,
        allows_write_to_pm=row.allows_write_to_pm,
        photo_url=row.photo_url,
    )


def apply_profile(entity: TelegramProfileEntity, row: TelegramProfileRow) -> None:
    """
    Copy a domain Telegram profile onto an ORM profile row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain profile with the values to persist.
        row: SQLAlchemy ``TelegramProfile`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.

    Notes:
        - Copies persisted columns only; the ``user`` relationship is never
          touched.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.telegram_id = entity.telegram_id
    row.is_bot = entity.is_bot
    row.first_name = entity.first_name
    row.last_name = entity.last_name
    row.username = entity.username
    row.language_code = entity.language_code
    row.is_premium = entity.is_premium
    row.added_to_attachment_menu = entity.added_to_attachment_menu
    row.allows_write_to_pm = entity.allows_write_to_pm
    row.photo_url = entity.photo_url


def auth_session_to_entity(row: AuthSessionRow) -> AuthSessionEntity:
    """
    Map an ORM auth session row to a domain session.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``AuthSession`` row.

    Returns:
        AuthSessionEntity: Domain session populated from persisted columns.
    """
    return AuthSessionEntity(
        id=row.id,
        user_id=row.user_id,
        family_id=row.family_id,
        refresh_token_hash=row.refresh_token_hash,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def apply_auth_session(entity: AuthSessionEntity, row: AuthSessionRow) -> None:
    """
    Copy a domain auth session onto an ORM session row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain session with the values to persist.
        row: SQLAlchemy ``AuthSession`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.family_id = entity.family_id
    row.refresh_token_hash = entity.refresh_token_hash
    row.created_at = entity.created_at
    row.expires_at = entity.expires_at
    row.revoked_at = entity.revoked_at


def color_to_entity(row: ColorPaletteRow) -> ColorPaletteEntity:
    """
    Map an ORM color palette row to a domain color.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``ColorPalette`` row.

    Returns:
        ColorPaletteEntity: Domain color populated from persisted columns.
    """
    return ColorPaletteEntity(
        id=row.id,
        slug=row.slug,
        name=row.name,
        hex=row.hex,
        sort_order=row.sort_order,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_color(entity: ColorPaletteEntity, row: ColorPaletteRow) -> None:
    """
    Copy a domain color onto an ORM color palette row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain color with the values to persist.
        row: SQLAlchemy ``ColorPalette`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.slug = entity.slug
    row.name = entity.name
    row.hex = entity.hex
    row.sort_order = entity.sort_order
    row.is_active = entity.is_active
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def label_to_entity(row: LabelRow) -> LabelEntity:
    """
    Map an ORM label row to a domain label.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``Label`` row.

    Returns:
        LabelEntity: Domain label populated from persisted columns.
    """
    return LabelEntity(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        color_id=row.color_id,
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_label(entity: LabelEntity, row: LabelRow) -> None:
    """
    Copy a domain label onto an ORM label row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain label with the values to persist.
        row: SQLAlchemy ``Label`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.name = entity.name
    row.color_id = entity.color_id
    row.deleted_at = entity.deleted_at
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def project_to_entity(row: ProjectRow) -> ProjectEntity:
    """
    Map an ORM project row to a domain project.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``Project`` row.

    Returns:
        ProjectEntity: Domain project populated from persisted columns.
    """
    return ProjectEntity(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        description=row.description,
        color_id=row.color_id,
        icon=row.icon,
        archived_at=row.archived_at,
        deleted_at=row.deleted_at,
        deletion_batch_id=row.deletion_batch_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_project(entity: ProjectEntity, row: ProjectRow) -> None:
    """
    Copy a domain project onto an ORM project row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain project with the values to persist.
        row: SQLAlchemy ``Project`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.name = entity.name
    row.description = entity.description
    row.color_id = entity.color_id
    row.icon = entity.icon
    row.archived_at = entity.archived_at
    row.deleted_at = entity.deleted_at
    row.deletion_batch_id = entity.deletion_batch_id
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def series_to_entity(row: TaskSeriesRow) -> TaskSeriesEntity:
    """
    Map an ORM task series row to a domain series.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``TaskSeries`` row.

    Returns:
        TaskSeriesEntity: Domain series populated from persisted columns.
    """
    return TaskSeriesEntity(
        id=row.id,
        user_id=row.user_id,
        project_id=row.project_id,
        title=row.title,
        description=row.description,
        priority=row.priority,
        frequency=row.frequency,
        interval_days=row.interval_days,
        time_of_day=row.time_of_day,
        timezone=row.timezone,
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        generated_through=row.generated_through,
        reminders_enabled=row.reminders_enabled,
        deleted_at=row.deleted_at,
        deletion_batch_id=row.deletion_batch_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_series(entity: TaskSeriesEntity, row: TaskSeriesRow) -> None:
    """
    Copy a domain task series onto an ORM series row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain series with the values to persist.
        row: SQLAlchemy ``TaskSeries`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.project_id = entity.project_id
    row.title = entity.title
    row.description = entity.description
    row.priority = entity.priority
    row.frequency = entity.frequency
    row.interval_days = entity.interval_days
    row.time_of_day = entity.time_of_day
    row.timezone = entity.timezone
    row.starts_on = entity.starts_on
    row.ends_on = entity.ends_on
    row.generated_through = entity.generated_through
    row.reminders_enabled = entity.reminders_enabled
    row.deleted_at = entity.deleted_at
    row.deletion_batch_id = entity.deletion_batch_id
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def task_to_entity(row: TaskRow) -> TaskEntity:
    """
    Map an ORM task row to a domain task.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``Task`` row.

    Returns:
        TaskEntity: Domain task populated from persisted columns.
    """
    return TaskEntity(
        id=row.id,
        user_id=row.user_id,
        series_id=row.series_id,
        project_id=row.project_id,
        title=row.title,
        description=row.description,
        priority=row.priority,
        due_at=row.due_at,
        completed_at=row.completed_at,
        reminders_enabled=row.reminders_enabled,
        deleted_at=row.deleted_at,
        deletion_batch_id=row.deletion_batch_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_task(entity: TaskEntity, row: TaskRow) -> None:
    """
    Copy a domain task onto an ORM task row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain task with the values to persist.
        row: SQLAlchemy ``Task`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.series_id = entity.series_id
    row.project_id = entity.project_id
    row.title = entity.title
    row.description = entity.description
    row.priority = entity.priority
    row.due_at = entity.due_at
    row.completed_at = entity.completed_at
    row.reminders_enabled = entity.reminders_enabled
    row.deleted_at = entity.deleted_at
    row.deletion_batch_id = entity.deletion_batch_id
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def reminder_to_entity(row: TaskReminderRow) -> TaskReminderEntity:
    """
    Map an ORM reminder row to a domain reminder.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``TaskReminder`` row.

    Returns:
        TaskReminderEntity: Domain reminder populated from persisted columns.
    """
    return TaskReminderEntity(
        id=row.id,
        user_id=row.user_id,
        task_id=row.task_id,
        series_id=row.series_id,
        kind=row.kind,
        enabled=row.enabled,
        offset_minutes=row.offset_minutes,
        interval_days=row.interval_days,
        time_of_day=row.time_of_day,
        timezone=row.timezone,
        next_run_at=row.next_run_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_reminder(entity: TaskReminderEntity, row: TaskReminderRow) -> None:
    """
    Copy a domain reminder onto an ORM reminder row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain reminder with the values to persist.
        row: SQLAlchemy ``TaskReminder`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.user_id = entity.user_id
    row.task_id = entity.task_id
    row.series_id = entity.series_id
    row.kind = entity.kind
    row.enabled = entity.enabled
    row.offset_minutes = entity.offset_minutes
    row.interval_days = entity.interval_days
    row.time_of_day = entity.time_of_day
    row.timezone = entity.timezone
    row.next_run_at = entity.next_run_at
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def delivery_to_entity(row: ReminderDeliveryRow) -> ReminderDeliveryEntity:
    """
    Map an ORM reminder delivery row to a domain delivery.

    Purpose:
        Convert a persistence row into the ORM-free entity consumed by services.

    Args:
        row: SQLAlchemy ``ReminderDelivery`` row.

    Returns:
        ReminderDeliveryEntity: Domain delivery populated from persisted columns.
    """
    return ReminderDeliveryEntity(
        id=row.id,
        reminder_id=row.reminder_id,
        user_id=row.user_id,
        scheduled_for=row.scheduled_for,
        status=row.status,
        attempts=row.attempts,
        locked_at=row.locked_at,
        next_attempt_at=row.next_attempt_at,
        sent_at=row.sent_at,
        telegram_message_id=row.telegram_message_id,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_delivery(entity: ReminderDeliveryEntity, row: ReminderDeliveryRow) -> None:
    """
    Copy a domain reminder delivery onto an ORM delivery row.

    Purpose:
        Keep an existing row in sync with a mutated domain entity before flush.

    Args:
        entity: Domain delivery with the values to persist.
        row: SQLAlchemy ``ReminderDelivery`` row updated in place.

    Returns:
        None: The row is updated in memory and is not flushed.
    """
    row.id = entity.id
    row.reminder_id = entity.reminder_id
    row.user_id = entity.user_id
    row.scheduled_for = entity.scheduled_for
    row.status = entity.status
    row.attempts = entity.attempts
    row.locked_at = entity.locked_at
    row.next_attempt_at = entity.next_attempt_at
    row.sent_at = entity.sent_at
    row.telegram_message_id = entity.telegram_message_id
    row.last_error = entity.last_error
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


def task_label_to_entity(row: TaskLabelRow) -> TaskLabelEntity:
    """
    Map an ORM task-label assignment row to a domain entity.

    Purpose:
        Convert a composite-key assignment row into the ORM-free entity used
        by services.

    Args:
        row: SQLAlchemy ``TaskLabel`` row.

    Returns:
        TaskLabelEntity: Domain assignment populated from persisted columns.
    """
    return TaskLabelEntity(
        task_id=row.task_id,
        label_id=row.label_id,
        user_id=row.user_id,
        created_at=row.created_at,
    )


def series_label_to_entity(row: SeriesLabelRow) -> SeriesLabelEntity:
    """
    Map an ORM series-label assignment row to a domain entity.

    Purpose:
        Convert a composite-key assignment row into the ORM-free entity used
        by services.

    Args:
        row: SQLAlchemy ``SeriesLabel`` row.

    Returns:
        SeriesLabelEntity: Domain assignment populated from persisted columns.
    """
    return SeriesLabelEntity(
        series_id=row.series_id,
        label_id=row.label_id,
        user_id=row.user_id,
        created_at=row.created_at,
    )
