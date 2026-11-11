from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backend.core.errors import BadRequestError
from backend.domain.entities import TaskReminder
from backend.domain.enums import ReminderKind
from backend.domain.task_management.errors import ReminderConfigurationError
from backend.domain.task_management.policies import (
    build_task_reminder,
    reminder_input_from_row,
)

__all__ = ["build_instance_reminder"]


def build_instance_reminder(
    template: TaskReminder,
    *,
    task_id: int,
    due_at: datetime,
    fallback_timezone: str,
    is_periodic: bool,
    now: datetime,
) -> TaskReminder | None:
    """
    Copy a series reminder template onto one generated instance.

    Purpose:
        Compute the instance-specific first slot while preserving the
        configured reminder kind and parameters; shared by series
        materialization and in-place future-instance updates.

    Args:
        template: Series template reminder.
        task_id: Generated task identifier.
        due_at: Generated task due instant.
        fallback_timezone: Series timezone used when the template lacks one.
        is_periodic: Whether the parent series uses a periodic frequency.
        now: Current instant used as the lower bound.

    Returns:
        TaskReminder | None: The instance reminder, or ``None`` when a
            before-due moment has already passed.

    Raises:
        BadRequestError: If the reminder schedule overflows the datetime
            range.

    Notes:
        - Periodic series anchor scheduled reminders to the instance's local
          due date, so a reminder earlier than the due time stays on the task
          day and advances by its own interval.
        - Daily series keep the due-relative rule unchanged.
        - Does not persist; the caller controls the unit of work.
    """
    timezone_name = template.timezone or fallback_timezone
    scheduled_starts_on = (
        due_at.astimezone(ZoneInfo(timezone_name)).date()
        if is_periodic and template.kind == ReminderKind.SCHEDULED.value
        else None
    )
    try:
        return build_task_reminder(
            reminder_input_from_row(template),
            user_id=template.user_id,
            task_id=task_id,
            due_at=due_at,
            timezone_name=timezone_name,
            now=now,
            scheduled_starts_on=scheduled_starts_on,
        )
    except ReminderConfigurationError as error:
        raise BadRequestError(str(error)) from error
