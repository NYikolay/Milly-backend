from __future__ import annotations

from datetime import datetime

from backend.application.ports.collaborators import ProjectReader
from backend.application.ports.task_management import (
    ReminderRepository,
    SeriesRepository,
    TaskRepository,
)
from backend.application.ports.unit_of_work import UnitOfWork
from backend.application.task_management.series_reminders import build_instance_reminder
from backend.application.task_management.task_references import TaskReferences
from backend.core.errors import BadRequestError
from backend.domain.entities import Task, TaskLabel, TaskReminder, TaskSeries
from backend.domain.enums import SeriesFrequency
from backend.domain.task_management.policies import series_due_times

_INVALID_RANGE = "Series end date must not be before the start date"

__all__ = ["SeriesMaterializer"]


class SeriesMaterializer:
    """Materializes due task instances of one recurring series."""

    def __init__(
        self,
        series_repository: SeriesRepository,
        task_repository: TaskRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        unit_of_work: UnitOfWork,
    ) -> None:
        """
        Initialize the materializer with persistence, validation, and time ports.

        Purpose:
            Share one-series occurrence generation between series creation,
            schedule edits, and the periodic generation job.

        Args:
            series_repository: Persistence reads and writes for series templates.
            task_repository: Persistence operations for generated task instances.
            reminder_repository: Persistence operations for template and
                instance reminders.
            project_reader: Active-project gate for the owning project.
            references: Timezone reference validation.
            unit_of_work: Transaction boundary used for direct flushes.

        Notes:
            - The materializer never commits; the caller owns the transaction
              boundary and decides when a generated series is committed.
        """
        self._series_repository = series_repository
        self._task_repository = task_repository
        self._reminder_repository = reminder_repository
        self._project_reader = project_reader
        self._references = references
        self._unit_of_work = unit_of_work

    async def materialize(self, series: TaskSeries, now: datetime) -> int:
        """
        Generate missing instances for one series.

        Purpose:
            Share the materialization rule between series creation, schedule
            edits, and the periodic generation job.

        Args:
            series: Series to materialize.
            now: Current instant used as the lower bound.

        Returns:
            int: Number of task instances created.

        Raises:
            BadRequestError: If the stored timezone is no longer valid, the
                schedule overflows the datetime range, or a reminder schedule
                overflows the datetime range.

        Notes:
            - Refuses to materialize instances when the series project is
              archived or deleted.
            - Uses the stored ``interval_days`` so periodic occurrences stay
              anchored to the series phase.
            - Copies snapshot fields, the series label set, and template
              reminders into every new instance.
            - Periodic series anchor scheduled instance reminders to each
              instance's due date; daily series use the due-relative rule.
            - Writes tasks, label rows, and instance reminders as three bulk
              batches with at most one flush each.
            - Leaves an existing ``generated_through`` unchanged when no new
              slot is produced, so resuming never rewinds the marker.
            - Persists a changed ``generated_through`` marker through
              ``save_series``; the caller's commit ends the transaction.
            - Flushes directly only when no task row is written.
            - Does not commit.
        """
        if series.project_id is not None and not await self._project_reader.project_is_active(
            series.user_id,
            series.project_id,
        ):
            return 0
        zone = self._references.named_timezone(series.timezone)
        try:
            due_times = series_due_times(
                starts_on=series.starts_on,
                ends_on=series.ends_on,
                time_of_day=series.time_of_day,
                timezone=zone,
                now=now,
                interval_days=series.interval_days,
                generated_through=series.generated_through,
            )
        except OverflowError as error:
            raise BadRequestError(_INVALID_RANGE) from error
        if not due_times:
            if series.generated_through is None:
                series.generated_through = now
                await self._series_repository.save_series(series)
            await self._unit_of_work.flush()
            return 0

        existing_due_times = set(
            await self._series_repository.list_series_due_times(
                series.id,
                series.user_id,
                now=now,
            )
        )
        labels = await self._series_repository.list_series_labels(series.user_id, series.id)
        template_reminders = await self._reminder_repository.list_series_reminders(
            series.user_id,
            series.id,
        )
        tasks: list[Task] = []
        last_due_at: datetime | None = None
        for due_at in due_times:
            last_due_at = due_at
            if due_at in existing_due_times:
                continue
            tasks.append(
                Task(
                    user_id=series.user_id,
                    series_id=series.id,
                    project_id=series.project_id,
                    title=series.title,
                    description=series.description,
                    priority=series.priority,
                    due_at=due_at,
                    completed_at=None,
                    reminders_enabled=series.reminders_enabled,
                )
            )
        created = len(tasks)
        if tasks:
            tasks = list(await self._task_repository.add_tasks(tasks))
            await self._task_repository.add_task_label_rows(
                [
                    TaskLabel(
                        task_id=task.id,
                        label_id=label.id,
                        user_id=series.user_id,
                    )
                    for task in tasks
                    for label in labels
                ]
            )
            instance_reminders: list[TaskReminder] = []
            for task in tasks:
                for template in template_reminders:
                    instance_reminder = build_instance_reminder(
                        template,
                        task_id=task.id,
                        due_at=task.due_at,
                        fallback_timezone=series.timezone,
                        is_periodic=series.frequency == SeriesFrequency.PERIODIC.value,
                        now=now,
                    )
                    if instance_reminder is None:
                        continue
                    instance_reminders.append(instance_reminder)
            await self._reminder_repository.add_reminders(instance_reminders)

        marker_changed = False
        if created:
            series.generated_through = last_due_at
            marker_changed = True
        elif series.generated_through is None:
            series.generated_through = now
            marker_changed = True
        if marker_changed:
            await self._series_repository.save_series(series)
        if not tasks:
            await self._unit_of_work.flush()
        return created
