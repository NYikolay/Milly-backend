"""Compatibility re-exports of reminder policies and the delivery dispatcher."""

from backend.application.ports.collaborators import ReminderSender as ReminderSender
from backend.application.task_management.reminder_presenter import (
    build_reminder_text as build_reminder_text,
)
from backend.application.task_management.reminders import (
    ReminderDispatcher as ReminderDispatcher,
)
from backend.domain.task_management.models import ReminderContent as ReminderContent
from backend.domain.task_management.models import ReminderInput as ReminderInput
from backend.domain.task_management.policies import (
    build_series_reminder as build_series_reminder,
)
from backend.domain.task_management.policies import (
    build_task_reminder as build_task_reminder,
)
from backend.domain.task_management.policies import (
    next_reminder_slot as next_reminder_slot,
)
from backend.domain.task_management.policies import (
    rebuild_reminder_slot as rebuild_reminder_slot,
)
from backend.domain.task_management.policies import (
    reminder_input_from_row as reminder_input_from_row,
)
from backend.domain.task_management.policies import (
    validate_reminder_inputs as validate_reminder_inputs,
)

__all__ = [
    "ReminderContent",
    "ReminderDispatcher",
    "ReminderInput",
    "ReminderSender",
    "build_reminder_text",
    "build_series_reminder",
    "build_task_reminder",
    "next_reminder_slot",
    "rebuild_reminder_slot",
    "reminder_input_from_row",
    "validate_reminder_inputs",
]
