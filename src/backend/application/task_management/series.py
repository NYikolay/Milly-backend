from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.application.ports.task_management import (
    ReminderRepository,
    SeriesRepository,
    TaskRepository,
)
from backend.application.ports.unit_of_work import UnitOfWork
from backend.application.task_management.series_materializer import SeriesMaterializer
from backend.application.task_management.series_reminders import build_instance_reminder
from backend.application.task_management.task_references import TaskReferences
from backend.core.errors import BadRequestError, NotFoundError
from backend.domain.entities import Task, TaskSeries
from backend.domain.enums import Priority, ReminderKind, SeriesFrequency
from backend.domain.task_management.errors import ReminderConfigurationError
from backend.domain.task_management.models import (
    UNSET,
    ReminderInput,
    SeriesDetail,
    UnsetType,
)
from backend.domain.task_management.policies import (
    build_series_reminder,
    reminder_input_from_row,
    require_supported_date,
    validate_reminder_inputs,
)

_SERIES_NOT_FOUND = "Series not found"
_TITLE_REQUIRED = "Series title is required"
_INVALID_RANGE = "Series end date must not be before the start date"
_TIME_OF_DAY_AWARE = "Time of day must be timezone-naive"
_INVALID_FREQUENCY_INTERVAL = "Series frequency and interval are incompatible"

__all__ = ["SeriesDetail", "SeriesService", "UNSET", "UnsetType"]


class SeriesService:
    """Coordinates recurring series creation, edits, and deletion."""

    def __init__(
        self,
        repository: SeriesRepository,
        task_repository: TaskRepository,
        reminder_repository: ReminderRepository,
        references: TaskReferences,
        materializer: SeriesMaterializer,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with persistence, validation, and time ports.

        Purpose:
            Keep series rules independent of request handling and make
            materialization deterministic through an injectable clock.

        Args:
            repository: Persistence operations for series templates.
            task_repository: Persistence operations for task instances.
            reminder_repository: Persistence operations for template and
                instance reminders.
            references: Project, label, and timezone reference validation.
            materializer: Occurrence generation for a single series.
            unit_of_work: Unit of work used for request-scoped flushes during
                series mutations.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - Request-scoped methods never commit.
        """
        self._repository = repository
        self._task_repository = task_repository
        self._reminder_repository = reminder_repository
        self._references = references
        self._materializer = materializer
        self._unit_of_work = unit_of_work
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def list(self, user_id: int) -> Sequence[TaskSeries]:
        """
        List non-deleted series owned by a user.

        Purpose:
            Provide the recurring-task list.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[TaskSeries]: Non-deleted series in repository order.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._repository.list_series(user_id)

    async def get(self, user_id: int, series_id: int) -> TaskSeries:
        """
        Load one owned series.

        Purpose:
            Resolve a series for a detail response.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            TaskSeries: The matching non-deleted series.

        Raises:
            NotFoundError: If the series does not exist for this user.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._require_series(user_id, series_id)

    async def list_details(self, user_id: int) -> Sequence[SeriesDetail]:
        """
        List owned series together with their template metadata.

        Purpose:
            Build the recurring-task list response with labels and template
            reminders.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[SeriesDetail]: Non-deleted series details in repository
                order.

        Notes:
            - Performs no writes and does not commit.
        """
        series_list = await self._repository.list_series(user_id)
        return [await self._detail(user_id, series) for series in series_list]

    async def get_detail(self, user_id: int, series_id: int) -> SeriesDetail:
        """
        Load one owned series with its template metadata.

        Purpose:
            Build a recurring-task detail response with labels and template
            reminders.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            SeriesDetail: The matching non-deleted series detail.

        Raises:
            NotFoundError: If the series does not exist for this user.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._detail(user_id, await self._require_series(user_id, series_id))

    async def create(
        self,
        user_id: int,
        *,
        title: str,
        description: str | None,
        priority: Priority,
        frequency: SeriesFrequency = SeriesFrequency.DAILY,
        interval_days: int | None = None,
        time_of_day: time,
        timezone: str,
        starts_on: date,
        ends_on: date | None,
        project_id: int | None,
        label_ids: Sequence[int],
        reminders_enabled: bool,
        reminders: Sequence[ReminderInput],
    ) -> TaskSeries:
        """
        Create a daily or periodic series and generate its first instances.

        Purpose:
            Persist the series template, labels, and template reminders, then
            materialize occurrences through the 90-day horizon without
            backfilling the past.

        Args:
            user_id: Owner identifier.
            title: Series title; surrounding whitespace is removed.
            description: Optional description snapshot.
            priority: Priority snapshot for generated instances.
            frequency: Daily or periodic occurrence rhythm. Defaults to daily.
            interval_days: Number of days between periodic occurrences; daily
                series accept only ``None`` or ``1`` and store ``1``. Defaults
                to ``None``.
            time_of_day: Local wall-clock occurrence time.
            timezone: IANA timezone of the series.
            starts_on: First local date on which the series may occur.
            ends_on: Optional inclusive final local date.
            project_id: Optional owned project.
            label_ids: Owned label identifiers copied to every instance.
            reminders_enabled: Master reminder toggle for generated instances.
            reminders: Template reminder rules.

        Returns:
            TaskSeries: The flushed series with its generated instances.

        Raises:
            BadRequestError: If the title is empty, the timezone is invalid,
                ``time_of_day`` is timezone-aware, the dates are outside the
                supported range or reversed, the frequency/interval pair is
                invalid, or a reminder rule is invalid.
            NotFoundError: If the project or a label does not exist for this
                user.

        Notes:
            - Periodic series require ``interval_days >= 2``; their phase is
              anchored to ``starts_on``.
            - Flushes through the ports but does not commit.
            - Callers should reject archived projects before creating series.
        """
        normalized_title = self._normalize_title(title)
        self._validate_time_of_day(time_of_day)
        zone = self._references.named_timezone(timezone)
        self._validate_range(starts_on, ends_on)
        resolved_frequency, resolved_interval_days = self._resolve_schedule(
            frequency=frequency,
            interval_days=interval_days,
        )
        await self._references.validate_project(user_id, project_id)
        owned_label_ids = await self._references.validate_labels(user_id, label_ids)
        try:
            validated_reminders = validate_reminder_inputs(reminders)
        except ReminderConfigurationError as error:
            raise BadRequestError(str(error)) from error
        series = TaskSeries(
            user_id=user_id,
            project_id=project_id,
            title=normalized_title,
            description=description,
            priority=priority.value,
            frequency=resolved_frequency.value,
            interval_days=resolved_interval_days,
            time_of_day=time_of_day,
            timezone=zone.key,
            starts_on=starts_on,
            ends_on=ends_on,
            generated_through=None,
            reminders_enabled=reminders_enabled,
        )
        series = await self._repository.add_series(series)
        await self._repository.add_series_labels(series.id, user_id, owned_label_ids)
        for reminder in validated_reminders:
            await self._reminder_repository.add_reminder(
                build_series_reminder(
                    reminder,
                    user_id=user_id,
                    series_id=series.id,
                    timezone_name=series.timezone,
                )
            )
        now = self._clock()
        await self._materializer.materialize(series, now)
        await self._unit_of_work.flush()
        return series

    async def create_from_task(
        self,
        user_id: int,
        task: Task,
        *,
        frequency: SeriesFrequency,
        interval_days: int | None = None,
    ) -> TaskSeries:
        """
        Convert a one-off task into a recurring series.

        Purpose:
            Snapshot the task into a new series template, attach the task to
            it, and materialize future instances without duplicating the
            current due slot.

        Args:
            user_id: Owner identifier.
            task: Task to convert; the caller must already have excluded an
                active series reference.
            frequency: Target daily or periodic occurrence rhythm.
            interval_days: Number of days between periodic occurrences; daily
                series accept only ``None`` or ``1``. Defaults to ``None``.

        Returns:
            TaskSeries: The flushed series with the converted task attached.

        Raises:
            BadRequestError: If the user's stored timezone is invalid, the
                frequency/interval pair is invalid, the derived start date is
                outside the supported range, the task project is archived, or
                generation overflows the supported calendar range.
            NotFoundError: If the task project or one of its labels does not
                exist for this user.

        Notes:
            - A ``task.series_id`` pointing to a soft-deleted series is
              overwritten by the new series.
            - ``starts_on`` and ``time_of_day`` come from ``task.due_at`` in
              the user's timezone, defaulting to UTC.
            - Scheduled template reminders receive the series interval only
              for periodic series; daily conversions keep the client interval.
            - Template reminders are stored without ``next_run_at`` so they
              never dispatch on their own.
            - Generated periodic instance reminders anchor scheduled slots to
              each instance's due date, while daily instances keep the
              due-relative rule.
            - The current task's instance reminders are left untouched.
            - The task is saved and the unit of work flushed before generation
              so the current due slot is visible to the dedupe query and is not
              duplicated.
            - Flushes through the ports but does not commit.
        """
        resolved_frequency, resolved_interval_days = self._resolve_schedule(
            frequency=frequency,
            interval_days=interval_days,
        )
        zone = await self._references.user_timezone(user_id)
        local_due_at = task.due_at.astimezone(zone)
        starts_on = local_due_at.date()
        time_of_day = local_due_at.time()
        self._validate_range(starts_on, None)
        await self._references.validate_project(user_id, task.project_id)
        labels = await self._task_repository.list_task_labels(user_id, task.id)
        owned_label_ids = await self._references.validate_labels(
            user_id,
            tuple(label.id for label in labels),
        )
        reminders = [
            reminder_input_from_row(reminder)
            for reminder in await self._reminder_repository.list_task_reminders(
                user_id,
                task.id,
            )
        ]
        if resolved_frequency is SeriesFrequency.PERIODIC:
            reminders = [
                replace(reminder, interval_days=resolved_interval_days)
                if reminder.kind is ReminderKind.SCHEDULED
                else reminder
                for reminder in reminders
            ]
        series = TaskSeries(
            user_id=user_id,
            project_id=task.project_id,
            title=task.title,
            description=task.description,
            priority=task.priority,
            frequency=resolved_frequency.value,
            interval_days=resolved_interval_days,
            time_of_day=time_of_day,
            timezone=zone.key,
            starts_on=starts_on,
            ends_on=None,
            generated_through=None,
            reminders_enabled=task.reminders_enabled,
        )
        series = await self._repository.add_series(series)
        await self._repository.add_series_labels(series.id, user_id, owned_label_ids)
        now = self._clock()
        for reminder in reminders:
            await self._reminder_repository.add_reminder(
                build_series_reminder(
                    reminder,
                    user_id=user_id,
                    series_id=series.id,
                    timezone_name=series.timezone,
                )
            )
        task.series_id = series.id
        await self._task_repository.save_task(task)
        await self._unit_of_work.flush()
        await self._materializer.materialize(series, now)
        await self._unit_of_work.flush()
        return series

    async def update(
        self,
        user_id: int,
        series_id: int,
        *,
        title: str | None = None,
        description: str | None | UnsetType = UNSET,
        priority: Priority | None = None,
        frequency: SeriesFrequency | None = None,
        interval_days: int | None = None,
        time_of_day: time | None = None,
        timezone: str | None = None,
        starts_on: date | None = None,
        ends_on: date | None | UnsetType = UNSET,
        project_id: int | None | UnsetType = UNSET,
        label_ids: Sequence[int] | None = None,
        reminders_enabled: bool | None = None,
        reminders: Sequence[ReminderInput] | None = None,
    ) -> TaskSeries:
        """
        Update a series and the future incomplete instances it owns.

        Purpose:
            Apply series-level edits without touching past or completed
            history; regenerate future instances when the schedule changes.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.
            title: New title; ``None`` keeps the current title.
            description: New description; omit to keep and pass ``None`` to
                clear it.
            priority: New priority snapshot.
            frequency: New frequency; ``None`` keeps the current frequency.
            interval_days: New number of days between periodic occurrences;
                ``None`` keeps the current interval for periodic series while
                daily series normalize to ``1``.
            time_of_day: New local occurrence time.
            timezone: New IANA timezone.
            starts_on: New first local date.
            ends_on: New optional final local date; omit to keep and pass
                ``None`` to clear it.
            project_id: New optional project; omit to keep and pass ``None`` to
                clear it.
            label_ids: Complete new label selection.
            reminders_enabled: New master reminder toggle.
            reminders: Complete new template reminder rules.

        Returns:
            TaskSeries: The updated series.

        Raises:
            NotFoundError: If the series, project, or a label does not exist for
                this user.
            BadRequestError: If the title is empty, the timezone is invalid,
                ``time_of_day`` is timezone-aware, the dates are outside the
                supported range or reversed, the frequency/interval pair is
                invalid, or a reminder rule is invalid.

        Notes:
            - A schedule change soft-deletes old future incomplete instances
              and regenerates replacements from the current time.
            - Changing the frequency or interval also counts as a schedule
              change; daily series store ``interval_days=1`` and periodic
              series require ``interval_days>=2``.
            - A timezone-only edit retargets template scheduled reminders to
              the new series zone before regeneration.
            - Regeneration refuses archived or deleted projects.
            - Other changes update future incomplete snapshots, labels, and
              reminders in place.
            - Locks the series row and its task rows; persists the mutated
              series and every changed future task explicitly; does not commit.
        """
        series = await self._require_series(user_id, series_id, for_update=True)
        resolved_frequency, resolved_interval_days = self._resolve_schedule(
            frequency=frequency,
            interval_days=interval_days,
            current_frequency=series.frequency,
            current_interval_days=series.interval_days,
        )
        if time_of_day is not None:
            self._validate_time_of_day(time_of_day)
        previous_reminders_enabled = series.reminders_enabled
        normalized_title = self._normalize_title(title) if title is not None else series.title
        zone = (
            self._references.named_timezone(timezone)
            if timezone is not None
            else ZoneInfo(series.timezone)
        )
        new_starts_on = starts_on if starts_on is not None else series.starts_on
        new_ends_on = series.ends_on if isinstance(ends_on, UnsetType) else ends_on
        self._validate_range(new_starts_on, new_ends_on)
        if not isinstance(project_id, UnsetType):
            await self._references.validate_project(user_id, project_id)
        owned_label_ids = (
            await self._references.validate_labels(user_id, label_ids)
            if label_ids is not None
            else None
        )
        if reminders is None:
            validated_reminders = None
        else:
            try:
                validated_reminders = validate_reminder_inputs(reminders)
            except ReminderConfigurationError as error:
                raise BadRequestError(str(error)) from error
        timezone_changed = zone.key != series.timezone
        frequency_changed = resolved_frequency.value != series.frequency
        interval_changed = resolved_interval_days != series.interval_days
        toggle_changed = (
            reminders_enabled is not None and reminders_enabled != previous_reminders_enabled
        )
        schedule_changed = (
            (time_of_day is not None and time_of_day != series.time_of_day)
            or timezone_changed
            or new_starts_on != series.starts_on
            or new_ends_on != series.ends_on
            or frequency_changed
            or interval_changed
        )

        series.title = normalized_title
        series.frequency = resolved_frequency.value
        series.interval_days = resolved_interval_days
        if not isinstance(description, UnsetType):
            series.description = description
        if priority is not None:
            series.priority = priority.value
        if time_of_day is not None:
            series.time_of_day = time_of_day
        if timezone is not None:
            series.timezone = zone.key
        if starts_on is not None:
            series.starts_on = starts_on
        if not isinstance(ends_on, UnsetType):
            series.ends_on = ends_on
        if not isinstance(project_id, UnsetType):
            series.project_id = project_id
        if reminders_enabled is not None:
            series.reminders_enabled = reminders_enabled
        await self._repository.save_series(series)

        if owned_label_ids is not None:
            await self._repository.replace_series_labels(
                series.id,
                user_id,
                owned_label_ids,
            )
        if validated_reminders is not None:
            await self._reminder_repository.delete_series_reminders(user_id, series.id)
            for reminder in validated_reminders:
                await self._reminder_repository.add_reminder(
                    build_series_reminder(
                        reminder,
                        user_id=user_id,
                        series_id=series.id,
                        timezone_name=series.timezone,
                    )
                )
        elif timezone_changed:
            for template in await self._reminder_repository.list_series_reminders(
                user_id,
                series.id,
            ):
                if template.kind == ReminderKind.SCHEDULED.value:
                    template.timezone = series.timezone
                    await self._reminder_repository.save_reminder(template)

        now = self._clock()
        if schedule_changed:
            locked_tasks = await self._repository.get_series_tasks_for_update(
                series.id,
                user_id,
            )
            future_tasks = [
                task for task in locked_tasks if task.due_at > now and task.completed_at is None
            ]
            batch_id = uuid4()
            for task in future_tasks:
                await self._reminder_repository.cancel_task_reminders(user_id, task.id)
                await self._task_repository.soft_delete_task(task, now, batch_id)
            series.generated_through = None
            await self._repository.save_series(series)
            await self._unit_of_work.flush()
            await self._materializer.materialize(series, now)
        else:
            await self._update_future_instances(
                user_id,
                series,
                now,
                title=title,
                description=description,
                priority=priority,
                project_id=project_id,
                label_ids=owned_label_ids,
                reminders_enabled=reminders_enabled,
                rebuild_reminders=(
                    validated_reminders is not None
                    or (toggle_changed and reminders_enabled is True)
                ),
                cancel_reminders=(toggle_changed and reminders_enabled is False),
            )
        await self._unit_of_work.flush()
        return series

    async def detach_task(
        self,
        user_id: int,
        task: Task,
        series: TaskSeries,
    ) -> None:
        """
        Detach one task from its series and delete the series.

        Purpose:
            Convert the selected instance back into a one-off task while
            removing future instances that would otherwise keep the series
            alive.

        Args:
            user_id: Owner identifier.
            task: Task to keep; the caller must already hold the series lock.
            series: Locked series to delete.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Physically deletes future incomplete instances except ``task``;
              past, completed, and overdue instances remain as history.
            - Cancels template reminder slots and deliveries.
            - Soft-deletes the series, clears and saves the current task's
              series reference, and does not commit.
            - Locks the series task rows through the repository query.
        """
        now = self._clock()
        locked_tasks = await self._repository.get_series_tasks_for_update(
            series.id,
            user_id,
        )
        removed_task_ids = tuple(
            item.id
            for item in locked_tasks
            if item.id != task.id and item.completed_at is None and item.due_at > now
        )
        await self._task_repository.hard_delete_tasks(removed_task_ids, user_id)
        await self._reminder_repository.cancel_series_reminders(user_id, series.id)
        await self._repository.soft_delete_series(series, now, uuid4())
        task.series_id = None
        await self._task_repository.save_task(task)
        await self._unit_of_work.flush()

    async def delete(
        self,
        user_id: int,
        series_id: int,
        *,
        target_task_id: int | None = None,
    ) -> None:
        """
        Soft-delete a series while physically removing future incomplete instances.

        Purpose:
            Stop generation and delivery, remove upcoming occurrences that no
            longer belong to a schedule, and preserve completed and past task
            history.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.
            target_task_id: Optional selected task to soft-delete when it
                survives hard deletion. Defaults to None.

        Returns:
            None: The deletion stays in the current unit of work.

        Raises:
            NotFoundError: If the series does not exist for this user.

        Notes:
            - Cancels reminder slots and deliveries for every task of the
              series before rows are removed or kept.
            - Physically deletes future incomplete instances (``completed_at``
              is ``None`` and ``due_at`` is later than the current time).
            - Soft-deletes the selected task with the series deletion batch
              when that task is not part of the hard-deleted set; completed
              and past instances stay as history.
            - Soft-deletes the series and clears template reminders.
            - Locks the series row and its task rows, and does not commit.
        """
        series = await self._require_series(user_id, series_id, for_update=True)
        now = self._clock()
        batch_id = uuid4()
        locked_tasks = await self._repository.get_series_tasks_for_update(
            series.id,
            user_id,
        )
        for task in locked_tasks:
            await self._reminder_repository.cancel_task_reminders(user_id, task.id)
        future_ids = tuple(
            task.id for task in locked_tasks if task.completed_at is None and task.due_at > now
        )
        await self._task_repository.hard_delete_tasks(future_ids, user_id)
        if target_task_id is not None and target_task_id not in future_ids:
            target = next(
                (task for task in locked_tasks if task.id == target_task_id),
                None,
            )
            if target is not None:
                await self._task_repository.soft_delete_task(target, now, batch_id)
        await self._reminder_repository.cancel_series_reminders(user_id, series.id)
        await self._repository.soft_delete_series(series, now, batch_id)
        await self._unit_of_work.flush()

    async def _update_future_instances(
        self,
        user_id: int,
        series: TaskSeries,
        now: datetime,
        *,
        title: str | None,
        description: str | None | UnsetType,
        priority: Priority | None,
        project_id: int | None | UnsetType,
        label_ids: Sequence[int] | None,
        reminders_enabled: bool | None,
        rebuild_reminders: bool,
        cancel_reminders: bool,
    ) -> None:
        """
        Update future incomplete instances in place.

        Purpose:
            Apply snapshot, label, and reminder changes to instances generated
            from the current series state.

        Args:
            user_id: Owner identifier.
            series: Updated series.
            now: Current instant used to rebuild reminder slots.
            title: New title, or ``None`` when unchanged.
            description: New description sentinel or value.
            priority: New priority, or ``None`` when unchanged.
            project_id: New project sentinel or value.
            label_ids: New label selection, or ``None`` when unchanged.
            reminders_enabled: New master toggle, or ``None`` when unchanged.
            rebuild_reminders: Whether instance reminders must be regenerated.
            cancel_reminders: Whether instance reminder slots must be cleared.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Locks the future instance rows through the repository query.
            - Cancellation takes precedence over a rebuild when both are set.
            - Every changed task is persisted through ``save_task``.
            - Does not commit.
        """
        locked_tasks = await self._repository.get_series_tasks_for_update(
            series.id,
            user_id,
        )
        future_tasks = [
            task for task in locked_tasks if task.due_at > now and task.completed_at is None
        ]
        template_reminders = (
            await self._reminder_repository.list_series_reminders(user_id, series.id)
            if rebuild_reminders and not cancel_reminders
            else ()
        )
        for task in future_tasks:
            changed = False
            if title is not None:
                task.title = series.title
                changed = True
            if not isinstance(description, UnsetType):
                task.description = series.description
                changed = True
            if priority is not None:
                task.priority = series.priority
                changed = True
            if not isinstance(project_id, UnsetType):
                task.project_id = series.project_id
                changed = True
            if reminders_enabled is not None:
                task.reminders_enabled = series.reminders_enabled
                changed = True
            if label_ids is not None:
                await self._task_repository.replace_task_labels(
                    task.id,
                    user_id,
                    label_ids,
                )
            if cancel_reminders:
                await self._reminder_repository.cancel_task_reminders(user_id, task.id)
            elif rebuild_reminders:
                await self._reminder_repository.cancel_task_reminders(user_id, task.id)
                await self._reminder_repository.delete_task_reminders(user_id, task.id)
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
                    await self._reminder_repository.add_reminder(instance_reminder)
            if changed:
                await self._task_repository.save_task(task)

    async def _require_series(
        self,
        user_id: int,
        series_id: int,
        *,
        for_update: bool = False,
    ) -> TaskSeries:
        """
        Load a series or raise a not-found error.

        Purpose:
            Treat missing series and another user's series identically.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            TaskSeries: The matching non-deleted series.

        Raises:
            NotFoundError: If no series matches the user and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        series = await self._repository.get_series(
            user_id,
            series_id,
            for_update=for_update,
        )
        if series is None:
            raise NotFoundError(_SERIES_NOT_FOUND)
        return series

    async def _detail(self, user_id: int, series: TaskSeries) -> SeriesDetail:
        """
        Load the labels and template reminders of a series.

        Purpose:
            Build the shared series-detail response shape.

        Args:
            user_id: Owner identifier.
            series: Series to enrich.

        Returns:
            SeriesDetail: The series with its related data.

        Notes:
            - Missing or deleted labels are omitted by the repository.
            - Performs no writes and does not commit.
        """
        labels = await self._repository.list_series_labels(user_id, series.id)
        reminders = await self._reminder_repository.list_series_reminders(user_id, series.id)
        return SeriesDetail(series=series, labels=labels, reminders=reminders)

    def _normalize_title(self, title: str) -> str:
        """
        Trim a series title and reject an empty result.

        Purpose:
            Apply the shared title rule before writes.

        Args:
            title: Candidate title.

        Returns:
            str: The trimmed title.

        Raises:
            BadRequestError: If the trimmed title is empty.
        """
        normalized = title.strip()
        if not normalized:
            raise BadRequestError(_TITLE_REQUIRED)
        return normalized

    def _validate_time_of_day(self, time_of_day: time) -> None:
        """
        Require a timezone-naive series wall-clock time.

        Purpose:
            Keep ``time_of_day`` a local wall-clock value so generation can
            resolve it through the series timezone.

        Args:
            time_of_day: Candidate local occurrence time.

        Returns:
            None: The value is timezone-naive.

        Raises:
            BadRequestError: If the value carries timezone information.
        """
        if time_of_day.tzinfo is not None or time_of_day.utcoffset() is not None:
            raise BadRequestError(_TIME_OF_DAY_AWARE)

    def _validate_range(self, starts_on: date, ends_on: date | None) -> None:
        """
        Validate the inclusive series date range.

        Purpose:
            Reject dates outside the supported bounds and an end date before
            the start date.

        Args:
            starts_on: First local date.
            ends_on: Optional inclusive final local date.

        Returns:
            None: The range is valid.

        Raises:
            BadRequestError: If either date is outside the inclusive supported
                range or ``ends_on`` is before ``starts_on``.
        """
        try:
            require_supported_date(starts_on)
            if ends_on is not None:
                require_supported_date(ends_on)
        except ValueError as error:
            raise BadRequestError(_INVALID_RANGE) from error
        if ends_on is not None and ends_on < starts_on:
            raise BadRequestError(_INVALID_RANGE)

    def _resolve_schedule(
        self,
        *,
        frequency: SeriesFrequency | None,
        interval_days: int | None,
        current_frequency: str | None = None,
        current_interval_days: int | None = None,
    ) -> tuple[SeriesFrequency, int]:
        """
        Resolve and validate a series frequency and interval request.

        Purpose:
            Enforce the daily/periodic invariant shared by series creation,
            conversion, and interval-only edits before persistence.

        Args:
            frequency: Requested frequency; ``None`` reuses ``current_frequency``
                for updates or defaults to daily for creation.
            interval_days: Requested interval; ``None`` reuses the current
                interval for periodic series and normalizes daily series to ``1``.
            current_frequency: Persisted frequency used when ``frequency`` is
                omitted. Defaults to ``None``.
            current_interval_days: Persisted interval used when ``interval_days``
                is omitted. Defaults to ``None``.

        Returns:
            tuple[SeriesFrequency, int]: The normalized frequency and interval.

        Raises:
            BadRequestError: If a daily series requests an interval other than
                ``1`` or a periodic series requests fewer than two days.

        Notes:
            - Daily series always store ``interval_days=1``.
            - Periodic series always store ``interval_days>=2``.
            - A conversion to periodic requires the new interval to be provided
              or the existing series to already be periodic.
            - Performs no writes.
        """
        target = frequency
        if target is None and current_frequency is not None:
            target = SeriesFrequency(current_frequency)
        target = target or SeriesFrequency.DAILY
        if target is SeriesFrequency.DAILY:
            if interval_days is not None and interval_days != 1:
                raise BadRequestError(_INVALID_FREQUENCY_INTERVAL)
            return SeriesFrequency.DAILY, 1
        resolved_interval = interval_days if interval_days is not None else current_interval_days
        if resolved_interval is None or resolved_interval < 2:
            raise BadRequestError(_INVALID_FREQUENCY_INTERVAL)
        return SeriesFrequency.PERIODIC, resolved_interval
