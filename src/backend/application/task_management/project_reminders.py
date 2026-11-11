"""Application commands that pause and resume project reminder delivery."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfoNotFoundError

from backend.application.ports.task_management import ReminderRepository
from backend.domain.enums import ReminderKind
from backend.domain.task_management.policies import rebuild_reminder_slot

__all__ = ["ProjectReminderService"]


class ProjectReminderService:
    """Pauses and resumes reminder delivery for an archived project."""

    def __init__(self, repository: ReminderRepository) -> None:
        """
        Initialize the service with its reminder persistence port.

        Purpose:
            Keep project reminder lifecycle policy in the application layer
            while reusing the aggregate reminder repository for reads and
            saves.

        Args:
            repository: Reminder persistence operations for contexts, batch
                saves, and the project bulk pause.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository

    async def pause_project(self, user_id: int, project_id: int) -> None:
        """
        Stop reminder delivery for an archived project.

        Purpose:
            Clear reminder slots and cancel active deliveries for the project's
            tasks and series while the project is archived.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Delegates to the repository bulk pause so no entity slots are
              touched in the application layer.
            - Flushes through the repository but does not commit.
        """
        await self._repository.pause_project_reminders(user_id, project_id)

    async def resume_project(self, user_id: int, project_id: int, now: datetime) -> None:
        """
        Recompute future reminder slots for an unarchived project.

        Purpose:
            Resume delivery when an archived project becomes active again
            without backfilling slots missed while it was paused.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.
            now: Current instant used as the lower bound for scheduled slots.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Template series reminders keep ``next_run_at=None`` and scheduled
              templates adopt the series timezone.
            - Incomplete task reminders, including overdue tasks, are
              recomputed: before-due slots come from ``due_at`` and become
              ``None`` when past; scheduled slots advance to the first suitable
              slot after ``now``.
            - A reminder whose stored timezone cannot be resolved or whose
              schedule overflows the datetime range keeps ``next_run_at=None``.
            - Cancelled deliveries are left for the dispatcher to reactivate
              when their recomputed slot becomes current.
            - Saves every changed reminder in one batch flush but does not
              commit.
        """
        contexts = await self._repository.list_project_reminder_contexts(user_id, project_id)
        for context in contexts:
            reminder = context.reminder
            if context.is_series:
                reminder.next_run_at = None
                if reminder.kind == ReminderKind.SCHEDULED.value:
                    reminder.timezone = context.series_timezone
                continue
            if context.task_due_at is None:
                reminder.next_run_at = None
                continue
            try:
                reminder.next_run_at = rebuild_reminder_slot(
                    reminder,
                    due_at=context.task_due_at,
                    now=now,
                )
            except (ZoneInfoNotFoundError, ValueError):  # fmt: skip
                reminder.next_run_at = None
        await self._repository.save_reminders([context.reminder for context in contexts])
