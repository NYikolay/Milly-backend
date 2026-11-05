from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from backend.core.errors import BadRequestError, NotFoundError
from backend.models.label import Label
from backend.models.task import Task
from backend.models.task_label import TaskLabel
from backend.models.task_reminder import TaskReminder
from backend.models.task_series import TaskSeries
from backend.task_management.date_bounds import require_supported_date
from backend.task_management.enums import Priority, ReminderKind, SeriesFrequency
from backend.task_management.labels import LabelService
from backend.task_management.ports import TaskManagementRepository
from backend.task_management.projects import ProjectService
from backend.task_management.reminders import (
    ReminderInput,
    build_series_reminder,
    build_task_reminder,
    reminder_input_from_row,
    validate_reminder_inputs,
)
from backend.task_management.schedule import HORIZON_DAYS, series_due_times

_SERIES_NOT_FOUND = "Series not found"
_LABEL_NOT_FOUND = "Label not found"
_TITLE_REQUIRED = "Series title is required"
_INVALID_TIMEZONE = "Invalid timezone"
_INVALID_RANGE = "Series end date must not be before the start date"
_PROJECT_ARCHIVED = "Project is archived"
_TIME_OF_DAY_AWARE = "Time of day must be timezone-naive"
_INVALID_FREQUENCY_INTERVAL = "Series frequency and interval are incompatible"

logger = structlog.get_logger(__name__)


class UnsetType:
    """Sentinel type for optional series update fields that were not provided."""


UNSET = UnsetType()


@dataclass(frozen=True)
class SeriesDetail:
    """A recurring series with its labels and template reminders."""

    series: TaskSeries
    labels: Sequence[Label]
    reminders: Sequence[TaskReminder]


class SeriesService:
    """Coordinates recurring series creation, generation, edits, and deletion."""

    def __init__(
        self,
        repository: TaskManagementRepository,
        project_service: ProjectService,
        label_service: LabelService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with persistence, validation, and time ports.

        Purpose:
            Keep series rules independent of request handling and make
            generation deterministic through an injectable clock.

        Args:
            repository: Persistence operations for series, tasks, and
                reminders.
            project_service: Project ownership and lifecycle validation.
            label_service: Label ownership and existence validation.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never commits; generation writes stay in the current
              unit of work.
        """
        self._repository = repository
        self._project_service = project_service
        self._label_service = label_service
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
            - Flushes through the repository but does not commit.
            - Callers should reject archived projects before creating series.
        """
        normalized_title = self._normalize_title(title)
        self._validate_time_of_day(time_of_day)
        zone = self._timezone(timezone)
        self._validate_range(starts_on, ends_on)
        resolved_frequency, resolved_interval_days = self._resolve_schedule(
            frequency=frequency,
            interval_days=interval_days,
        )
        await self._validate_project(user_id, project_id)
        owned_label_ids = await self._validate_labels(user_id, label_ids)
        validated_reminders = validate_reminder_inputs(reminders)
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
        await self._repository.add_series(series)
        await self._repository.add_series_labels(series.id, user_id, owned_label_ids)
        for reminder in validated_reminders:
            await self._repository.add_reminder(
                build_series_reminder(
                    reminder,
                    user_id=user_id,
                    series_id=series.id,
                    timezone_name=series.timezone,
                )
            )
        now = self._clock()
        await self._generate_series(series, now)
        await self._repository.flush()
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
                frequency/interval pair is invalid, or the task project is
                archived.
            NotFoundError: If the task project or one of its labels does not
                exist for this user.

        Notes:
            - A ``task.series_id`` pointing to a soft-deleted series is
              overwritten by the new series.
            - ``starts_on`` and ``time_of_day`` come from ``task.due_at`` in
              the user's timezone, defaulting to UTC.
            - Scheduled template reminders receive the series interval only
              for periodic series; daily conversions keep the client interval.
            - The current task's instance reminders are left untouched.
            - Sets ``task.series_id`` before generation so the current due slot
              is not duplicated.
            - Flushes through the repository but does not commit.
        """
        resolved_frequency, resolved_interval_days = self._resolve_schedule(
            frequency=frequency,
            interval_days=interval_days,
        )
        timezone_name = await self._repository.get_user_timezone(user_id)
        zone = self._timezone(timezone_name or "UTC")
        local_due_at = task.due_at.astimezone(zone)
        starts_on = local_due_at.date()
        time_of_day = local_due_at.time()
        await self._validate_project(user_id, task.project_id)
        labels = await self._repository.list_task_labels(user_id, task.id)
        owned_label_ids = await self._validate_labels(
            user_id,
            tuple(label.id for label in labels),
        )
        reminders = [
            reminder_input_from_row(reminder)
            for reminder in await self._repository.list_task_reminders(user_id, task.id)
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
        await self._repository.add_series(series)
        await self._repository.add_series_labels(series.id, user_id, owned_label_ids)
        for reminder in reminders:
            await self._repository.add_reminder(
                build_series_reminder(
                    reminder,
                    user_id=user_id,
                    series_id=series.id,
                    timezone_name=series.timezone,
                )
            )
        task.series_id = series.id
        now = self._clock()
        await self._generate_series(series, now)
        await self._repository.flush()
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
            - Locks the series row and its task rows, and does not commit.
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
        zone = self._timezone(timezone) if timezone is not None else ZoneInfo(series.timezone)
        new_starts_on = starts_on if starts_on is not None else series.starts_on
        new_ends_on = series.ends_on if isinstance(ends_on, UnsetType) else ends_on
        self._validate_range(new_starts_on, new_ends_on)
        if not isinstance(project_id, UnsetType):
            await self._validate_project(user_id, project_id)
        owned_label_ids = (
            await self._validate_labels(user_id, label_ids) if label_ids is not None else None
        )
        validated_reminders = validate_reminder_inputs(reminders) if reminders is not None else None
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

        if owned_label_ids is not None:
            await self._repository.replace_series_labels(
                series.id,
                user_id,
                owned_label_ids,
            )
        if validated_reminders is not None:
            await self._repository.delete_series_reminders(user_id, series.id)
            for reminder in validated_reminders:
                await self._repository.add_reminder(
                    build_series_reminder(
                        reminder,
                        user_id=user_id,
                        series_id=series.id,
                        timezone_name=series.timezone,
                    )
                )
        elif timezone_changed:
            for template in await self._repository.list_series_reminders(user_id, series.id):
                if template.kind == ReminderKind.SCHEDULED.value:
                    template.timezone = series.timezone

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
                await self._repository.cancel_task_reminders(user_id, task.id)
                await self._repository.soft_delete_task(task, now, batch_id)
            series.generated_through = None
            await self._repository.flush()
            await self._generate_series(series, now)
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
        await self._repository.flush()
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
            - Soft-deletes the series and clears the current task's series
              reference.
            - Locks the series task rows through the repository query and does
              not commit.
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
        await self._repository.hard_delete_tasks(removed_task_ids, user_id)
        await self._repository.cancel_series_reminders(user_id, series.id)
        await self._repository.soft_delete_series(series, now, uuid4())
        task.series_id = None
        await self._repository.flush()

    async def delete(self, user_id: int, series_id: int) -> None:
        """
        Soft-delete a series and its future incomplete instances.

        Purpose:
            Stop generation and delivery while preserving past task history.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            None: The deletion stays in the current unit of work.

        Raises:
            NotFoundError: If the series does not exist for this user.

        Notes:
            - Cancels reminder slots and deliveries for every task of the
              series, including overdue incomplete instances, while keeping
              their task rows as history.
            - Soft-deletes only future incomplete instances and the series,
              and clears template reminders.
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
            await self._repository.cancel_task_reminders(user_id, task.id)
        for task in locked_tasks:
            if task.due_at <= now or task.completed_at is not None:
                continue
            await self._repository.soft_delete_task(task, now, batch_id)
        await self._repository.cancel_series_reminders(user_id, series.id)
        await self._repository.soft_delete_series(series, now, batch_id)
        await self._repository.flush()

    async def generate(self, now: datetime) -> int:
        """
        Generate missing instances for every eligible series.

        Purpose:
            Run the periodic generation job without creating duplicates or
            materializing occurrences for paused projects.

        Args:
            now: Current instant used for the horizon and first future slot.

        Returns:
            int: Number of task instances created.

        Notes:
            - Selects non-deleted series whose project is active, whose
              ``generated_through`` is missing or earlier than
              ``now + 90 days - 1 day``, and whose ``ends_on`` is missing or
              not earlier than the previous UTC day.
            - Claims each series with ``FOR UPDATE SKIP LOCKED``; a series held
              by another worker is skipped until the next run.
            - Commits after each series so a later failure does not roll back
              earlier successful work and row locks are released promptly.
            - Candidate ids are snapshotted before processing so rollback does
              not require touching expired ORM objects.
            - Unexpected per-series errors roll back and continue with the next
              series.
            - Idempotent for slots already present in the repository.
            - Sets ``generated_through`` to the last generated due time; it only
              falls back to ``now`` while the series has no generation marker
              yet.
        """
        created = 0
        horizon = now + timedelta(days=HORIZON_DAYS)
        candidates = [
            (series.id, series.user_id)
            for series in await self._repository.list_series_for_generation(now)
        ]
        for series_id, series_user_id in candidates:
            try:
                series = await self._repository.claim_series_for_generation(
                    series_id,
                    series_user_id,
                )
                if series is None:
                    await self._repository.commit()
                    continue
                if series.generated_through is not None and series.generated_through >= horizon:
                    await self._repository.commit()
                    continue
                if series.project_id is not None and not await self._repository.project_is_active(
                    series.user_id,
                    series.project_id,
                ):
                    await self._repository.commit()
                    continue
                created += await self._generate_series(series, now)
                await self._repository.commit()
            except Exception as error:
                await self._repository.rollback()
                logger.warning(
                    "task.series_generation_failed",
                    series_id=series_id,
                    error=type(error).__name__,
                )
        return created

    async def _generate_series(self, series: TaskSeries, now: datetime) -> int:
        """
        Generate missing instances for one series.

        Purpose:
            Share the materialization rule between series creation and the
            periodic generation job.

        Args:
            series: Series to materialize.
            now: Current instant used as the lower bound.

        Returns:
            int: Number of task instances created.

        Raises:
            BadRequestError: If the stored timezone is no longer valid or the
                schedule overflows the datetime range.

        Notes:
            - Refuses to materialize instances when the series project is
              archived or deleted.
            - Uses the stored ``interval_days`` so periodic occurrences stay
              anchored to the series phase.
            - Copies snapshot fields, the series label set, and template
              reminders into every new instance.
            - Writes tasks, label rows, and instance reminders as three bulk
              batches with at most one flush each.
            - Leaves an existing ``generated_through`` unchanged when no new
              slot is produced, so resuming never rewinds the marker.
            - The ``generated_through`` marker after bulk writes is persisted
              by the caller's flush; this method flushes only no-task paths.
            - Does not commit.
        """
        if series.project_id is not None and not await self._repository.project_is_active(
            series.user_id,
            series.project_id,
        ):
            return 0
        zone = self._timezone(series.timezone)
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
            await self._repository.flush()
            return 0

        existing_due_times = set(
            await self._repository.list_series_due_times(series.id, series.user_id, now=now)
        )
        labels = await self._repository.list_series_labels(series.user_id, series.id)
        template_reminders = await self._repository.list_series_reminders(
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
            tasks = list(await self._repository.add_tasks(tasks))
            await self._repository.add_task_label_rows(
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
                    instance_reminder = self._instance_reminder(
                        template,
                        task_id=task.id,
                        due_at=task.due_at,
                        fallback_timezone=series.timezone,
                        now=now,
                    )
                    if instance_reminder is None:
                        continue
                    instance_reminders.append(instance_reminder)
            await self._repository.add_reminders(instance_reminders)

        if created:
            series.generated_through = last_due_at
        elif series.generated_through is None:
            series.generated_through = now
        if not tasks:
            await self._repository.flush()
        return created

    def _instance_reminder(
        self,
        template: TaskReminder,
        *,
        task_id: int,
        due_at: datetime,
        fallback_timezone: str,
        now: datetime,
    ) -> TaskReminder | None:
        """
        Copy a series reminder template onto one generated instance.

        Purpose:
            Compute the instance-specific first slot while preserving the
            configured reminder kind and parameters.

        Args:
            template: Series template reminder.
            task_id: Generated task identifier.
            due_at: Generated task due instant.
            fallback_timezone: Series timezone used when the template lacks one.
            now: Current instant used as the lower bound.

        Returns:
            TaskReminder | None: The instance reminder, or ``None`` when a
                before-due moment has already passed.
        """
        return build_task_reminder(
            reminder_input_from_row(template),
            user_id=template.user_id,
            task_id=task_id,
            due_at=due_at,
            timezone_name=template.timezone or fallback_timezone,
            now=now,
        )

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
            await self._repository.list_series_reminders(user_id, series.id)
            if rebuild_reminders and not cancel_reminders
            else ()
        )
        for task in future_tasks:
            if title is not None:
                task.title = series.title
            if not isinstance(description, UnsetType):
                task.description = series.description
            if priority is not None:
                task.priority = series.priority
            if not isinstance(project_id, UnsetType):
                task.project_id = series.project_id
            if reminders_enabled is not None:
                task.reminders_enabled = series.reminders_enabled
            if label_ids is not None:
                await self._repository.replace_task_labels(
                    task.id,
                    user_id,
                    label_ids,
                )
            if cancel_reminders:
                await self._repository.cancel_task_reminders(user_id, task.id)
            elif rebuild_reminders:
                await self._repository.cancel_task_reminders(user_id, task.id)
                await self._repository.delete_task_reminders(user_id, task.id)
                for template in template_reminders:
                    instance_reminder = self._instance_reminder(
                        template,
                        task_id=task.id,
                        due_at=task.due_at,
                        fallback_timezone=series.timezone,
                        now=now,
                    )
                    if instance_reminder is None:
                        continue
                    await self._repository.add_reminder(instance_reminder)

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
        reminders = await self._repository.list_series_reminders(user_id, series.id)
        return SeriesDetail(series=series, labels=labels, reminders=reminders)

    async def _validate_project(self, user_id: int, project_id: int | None) -> None:
        """
        Validate an optional project reference.

        Purpose:
            Reject missing, cross-user, deleted, and archived projects before
            writing or generating instances.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier or ``None``.

        Returns:
            None: The project is valid or absent.

        Raises:
            NotFoundError: If the project does not exist for this user.
            BadRequestError: If the project is archived.
        """
        if project_id is None:
            return
        project = await self._project_service.get(user_id, project_id)
        if project.archived_at is not None:
            raise BadRequestError(_PROJECT_ARCHIVED)

    async def _validate_labels(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> tuple[int, ...]:
        """
        Validate a label selection and remove duplicates.

        Purpose:
            Ensure every assigned label exists for the user before writing.

        Args:
            user_id: Owner identifier.
            label_ids: Requested label identifiers.

        Returns:
            tuple[int, ...]: Unique owned label identifiers in input order.

        Raises:
            NotFoundError: If any label does not exist for this user.

        Notes:
            - Performs one batch ownership and existence query.
        """
        unique_ids = tuple(dict.fromkeys(label_ids))
        labels = await self._label_service.list_by_ids(user_id, unique_ids)
        if len(labels) != len(unique_ids):
            raise NotFoundError(_LABEL_NOT_FOUND)
        return unique_ids

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

    def _timezone(self, name: str) -> ZoneInfo:
        """
        Resolve an IANA timezone name.

        Purpose:
            Validate series and reminder timezone configuration.

        Args:
            name: IANA timezone name.

        Returns:
            ZoneInfo: The resolved timezone.

        Raises:
            BadRequestError: If the timezone name is invalid.
        """
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise BadRequestError(_INVALID_TIMEZONE) from error

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
