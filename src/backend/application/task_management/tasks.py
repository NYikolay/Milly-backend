from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.application.ports.collaborators import ProjectReader, SeriesOperations
from backend.application.ports.task_management import (
    ReminderRepository,
    SeriesRepository,
    TaskRepository,
)
from backend.application.ports.unit_of_work import UnitOfWork
from backend.application.task_management.task_details import TaskDetails
from backend.application.task_management.task_inputs import (
    normalize_task_title,
    validate_recurrence_interval,
    validate_task_due_at,
)
from backend.application.task_management.task_references import TaskReferences
from backend.core.errors import BadRequestError, NotFoundError
from backend.domain.entities import Task, TaskSeries
from backend.domain.enums import Priority, SeriesFrequency, TaskRecurrence
from backend.domain.task_management.errors import ReminderConfigurationError
from backend.domain.task_management.models import (
    UNSET,
    ReminderInput,
    TaskDetail,
    TaskFilters,
    TaskListCursor,
    TaskPage,
    UnsetType,
)
from backend.domain.task_management.policies import (
    build_task_reminder,
    localize_wall_time,
    next_occurrence_after_date,
    rebuild_reminder_slot,
    reminder_input_from_row,
    validate_reminder_inputs,
)

_TASK_NOT_FOUND = "Task not found"
_INVALID_DATE_RANGE = "Date range is out of bounds"
_INVALID_RECURRENCE_INTERVAL = "Task recurrence and interval are incompatible"
_PAST_DUE_DATE = "Task due date is in the past"
_PAST_DUE_DATE_DESCRIPTION = (
    "Нельзя вернуть в работу задачу с прошедшей датой. Сначала измените дату задачи."
)

__all__ = ["TaskDetail", "TaskFilters", "TaskListCursor", "TaskPage", "TaskService"]


class TaskService:
    """Coordinates one-off tasks and series-instance operations."""

    def __init__(
        self,
        repository: TaskRepository,
        series_repository: SeriesRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        details: TaskDetails,
        series_operations: SeriesOperations,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with persistence, collaborators, and time ports.

        Purpose:
            Keep task rules independent of request handling and make
            timestamps deterministic through an injectable clock.

        Args:
            repository: Persistence operations for tasks and task-label rows.
            series_repository: Persistence reads for owning series.
            reminder_repository: Persistence operations for task reminders.
            project_reader: Project ownership and lifecycle validation plus the
                today progress aggregate.
            references: Project, label, and timezone reference validation.
            details: Shared task-detail assembly.
            series_operations: Series conversion, edit, detach, and delete rules.
            unit_of_work: Transaction boundary owned by the caller.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository
        self._series_repository = series_repository
        self._reminder_repository = reminder_repository
        self._project_reader = project_reader
        self._references = references
        self._details = details
        self._series_operations = series_operations
        self._unit_of_work = unit_of_work
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def list(
        self,
        user_id: int,
        filters: TaskFilters,
        *,
        limit: int,
        after: TaskListCursor | None = None,
    ) -> TaskPage:
        """
        List one page of task details for a user.

        Purpose:
            Provide the filtered task page with labels, reminders, and project
            data loaded through batch queries.

        Args:
            user_id: Owner identifier.
            filters: Date, project, priority, status, recurrence, text, and
                archive filters.
            limit: Maximum number of task details to return.
            after: Optional ordering key of the last task on the previous page.

        Returns:
            TaskPage: Matching task details in ``(due_at, id)`` order and the
                cursor for the next page, or ``None`` when this is the last
                page.

        Raises:
            BadRequestError: If the stored user timezone is invalid or the date
                filter overflows the supported calendar range.

        Notes:
            - The date filter uses UTC bounds of the user's local day.
            - The repository is queried for ``limit + 1`` rows so the extra row
              only signals another page.
            - Related labels, reminders, projects, and series are loaded with
              at most four batch queries; the series query is skipped when no
              task has a series reference, and an empty page issues no batch
              queries.
            - Performs no writes and does not commit.
        """
        date_start: datetime | None = None
        date_end: datetime | None = None
        if filters.date is not None:
            zone = await self._references.user_timezone(user_id)
            date_start, date_end = self._local_day_bounds(zone, filters.date)
        tasks = await self._repository.list_tasks(
            user_id,
            date_start=date_start,
            date_end=date_end,
            project_ids=filters.project_ids,
            priorities=tuple(priority.value for priority in filters.priorities),
            status=filters.status,
            recurrence=filters.recurrence,
            query=filters.query,
            include_archived_projects=filters.include_archived_projects,
            now=self._clock(),
            limit=limit + 1,
            after_due_at=after.due_at if after is not None else None,
            after_task_id=after.task_id if after is not None else None,
        )
        page_tasks = tuple(tasks[:limit])
        next_cursor = (
            TaskListCursor(due_at=page_tasks[-1].due_at, task_id=page_tasks[-1].id)
            if len(tasks) > limit and page_tasks
            else None
        )
        return TaskPage(
            items=await self._details.details(user_id, page_tasks),
            next_cursor=next_cursor,
        )

    async def today_progress(self, user_id: int) -> tuple[int, int]:
        """
        Count the user's tasks due today and how many are completed.

        Purpose:
            Build the dashboard "today" progress summary from the user's local
            calendar day.

        Args:
            user_id: Owner identifier.

        Returns:
            tuple[int, int]: Total non-deleted tasks due within the user's local
                day and the subset with a completion timestamp.

        Raises:
            BadRequestError: If the stored user timezone is invalid or the day
                bounds overflow the supported calendar range.

        Notes:
            - The current instant comes from the service clock and is converted
              to the user's timezone to derive the local date.
            - Local day bounds are converted to UTC before the repository query.
            - Performs no writes and does not commit.
        """
        zone = await self._references.user_timezone(user_id)
        local_date = self._clock().astimezone(zone).date()
        start, end = self._local_day_bounds(zone, local_date)
        return await self._project_reader.today_progress(user_id, start, end)

    async def get(self, user_id: int, task_id: int) -> TaskDetail:
        """
        Load one owned task detail.

        Purpose:
            Resolve a task for a detail response.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            TaskDetail: The matching task with labels, reminders, project, and
                recurrence.

        Raises:
            NotFoundError: If the task does not exist for this user.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._details.detail(user_id, await self._require_task(user_id, task_id))

    async def create(
        self,
        user_id: int,
        *,
        title: str,
        description: str | None,
        priority: Priority,
        due_at: datetime,
        project_id: int | None,
        label_ids: Sequence[int],
        reminders_enabled: bool,
        reminders: Sequence[ReminderInput],
    ) -> TaskDetail:
        """
        Create a one-off task with labels and reminders.

        Purpose:
            Persist a validated task and compute the first reminder slots from
            its due time and the current time.

        Args:
            user_id: Owner identifier.
            title: Task title; surrounding whitespace is removed.
            description: Optional description.
            priority: Task priority.
            due_at: Timezone-aware due instant.
            project_id: Optional owned project.
            label_ids: Owned label identifiers.
            reminders_enabled: Master reminder toggle.
            reminders: Reminder rules.

        Returns:
            TaskDetail: The flushed task with labels, reminders, and project.

        Raises:
            BadRequestError: If the title is empty, the due time is naive or
                outside the supported range, the timezone is invalid, the
                project is archived, or a reminder rule is invalid.
            NotFoundError: If the project or a label does not exist for this
                user.

        Notes:
            - Past before-due moments are skipped.
            - Scheduled reminders start from the first local slot on or after
              the due date and always land in the future.
            - Consumes the task returned by ``add_task`` for its generated
              identifier, then persists labels and reminders through the task
              and reminder ports.
            - Flushes through the unit of work but does not commit.
        """
        normalized_title = normalize_task_title(title)
        validate_task_due_at(due_at)
        await self._references.validate_project(user_id, project_id)
        owned_label_ids = await self._references.validate_labels(user_id, label_ids)
        try:
            validated_reminders = validate_reminder_inputs(reminders)
        except ReminderConfigurationError as error:
            raise BadRequestError(str(error)) from error
        zone = await self._references.user_timezone(user_id)
        now = self._clock()
        task = Task(
            user_id=user_id,
            series_id=None,
            project_id=project_id,
            title=normalized_title,
            description=description,
            priority=priority.value,
            due_at=due_at,
            completed_at=None,
            reminders_enabled=reminders_enabled,
        )
        task = await self._repository.add_task(task)
        await self._repository.add_task_labels(task.id, user_id, owned_label_ids)
        for reminder in validated_reminders:
            try:
                row = build_task_reminder(
                    reminder,
                    user_id=user_id,
                    task_id=task.id,
                    due_at=due_at,
                    timezone_name=zone.key,
                    now=now,
                )
            except ReminderConfigurationError as error:
                raise BadRequestError(str(error)) from error
            if row is not None:
                await self._reminder_repository.add_reminder(row)
        await self._unit_of_work.flush()
        return await self._details.detail(user_id, task)

    async def update(
        self,
        user_id: int,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None | UnsetType = UNSET,
        priority: Priority | None = None,
        due_at: datetime | None = None,
        project_id: int | None | UnsetType = UNSET,
        label_ids: Sequence[int] | None = None,
        reminders_enabled: bool | None = None,
        reminders: Sequence[ReminderInput] | None = None,
        recurrence: TaskRecurrence | None = None,
        interval_days: int | None = None,
    ) -> TaskDetail:
        """
        Update a task, its recurrence mode, or its recurring series.

        Purpose:
            Apply snapshot, label, due-time, reminder, and recurrence changes;
            a task toggled between one-off and series modes keeps its history
            while an overdue current instance moves to the next phase slot.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.
            title: New title; ``None`` keeps the current title.
            description: New description; omit to keep and pass ``None`` to
                clear it.
            priority: New priority; ``None`` keeps the current priority.
            due_at: New timezone-aware due instant; ``None`` keeps the current
                due time.
            project_id: New optional project; omit to keep and pass ``None`` to
                clear it.
            label_ids: Complete new label selection.
            reminders_enabled: New master reminder toggle.
            reminders: Complete new reminder rules.
            recurrence: Target recurrence mode; ``None`` keeps the current
                mode and treats ``interval_days`` as an interval-only edit.
            interval_days: Number of days between periodic occurrences; daily
                series accept only ``None`` or ``1``. Defaults to ``None``.

        Returns:
            TaskDetail: The updated task detail with its recurrence mode.

        Raises:
            NotFoundError: If the task, project, label, or series does not
                exist for this user.
            BadRequestError: If the title is empty, the due time is naive or
                outside the supported range, the project is archived, a
                reminder rule is invalid, or the recurrence/interval pair is
                incompatible.

        Notes:
            - Other fields from the same update are applied before a conversion
              takes its series snapshot.
            - Converting to a series, or changing ``due_at`` on an incomplete
              task whose series is soft-deleted, clears the stale series
              reference before the edit to avoid the unique-slot conflict.
              A completed task keeps its historical link when the new slot is
              free; a slot collision clears it. A field-only edit keeps the
              historical link.
            - Disabling recurrence detaches first, physically removes future
              incomplete siblings, then shifts the current overdue instance by
              the captured series phase.
            - An overdue incomplete current instance moves strictly after the
              current local day while preserving its local time.
            - An active series instance is edited through the series rule; a
              soft-deleted series is treated as a one-off task.
            - For a series instance the series row is locked before the task
              row; one-off tasks lock only the task row.
            - Does not commit.
        """
        try:
            validate_recurrence_interval(recurrence, interval_days)
            task, series = await self._lock_update_target(user_id, task_id)

            if series is None:
                return await self._update_detached_task(
                    user_id,
                    task,
                    title=title,
                    description=description,
                    priority=priority,
                    due_at=due_at,
                    project_id=project_id,
                    label_ids=label_ids,
                    reminders_enabled=reminders_enabled,
                    reminders=reminders,
                    recurrence=recurrence,
                    interval_days=interval_days,
                )

            if recurrence is TaskRecurrence.ONE_OFF:
                anchor = series.starts_on
                series_interval_days = series.interval_days
                zone = ZoneInfo(series.timezone)
                await self._series_operations.detach_task(user_id, task, series)
                now = self._clock()
                final_due_at = due_at if due_at is not None else task.due_at
                will_shift = (
                    task.completed_at is None
                    and final_due_at.tzinfo is not None
                    and final_due_at.utcoffset() is not None
                    and final_due_at <= now
                )
                await self._apply_task_update(
                    user_id,
                    task,
                    title=title,
                    description=description,
                    priority=priority,
                    due_at=due_at,
                    project_id=project_id,
                    label_ids=label_ids,
                    reminders_enabled=reminders_enabled,
                    reminders=reminders,
                    defer_reminder_rebuild=will_shift,
                )
                if will_shift:
                    await self._shift_overdue_task(
                        user_id,
                        task,
                        anchor=anchor,
                        interval_days=series_interval_days,
                        timezone=zone,
                        reminders=reminders,
                    )
                await self._repository.save_task(task)
                await self._unit_of_work.flush()
                return await self._details.detail(user_id, task)

            if (
                recurrence is None
                and interval_days is not None
                and series.frequency != SeriesFrequency.PERIODIC.value
            ):
                raise BadRequestError(_INVALID_RECURRENCE_INTERVAL)
            frequency: SeriesFrequency | None = None
            resolved_interval_days = interval_days
            if recurrence is TaskRecurrence.REGULAR:
                frequency = SeriesFrequency.DAILY
                resolved_interval_days = 1
            elif recurrence is TaskRecurrence.PERIODIC:
                frequency = SeriesFrequency.PERIODIC
            return await self._update_series_task(
                user_id,
                task,
                series,
                title=title,
                description=description,
                priority=priority,
                due_at=due_at,
                project_id=project_id,
                label_ids=label_ids,
                reminders_enabled=reminders_enabled,
                reminders=reminders,
                frequency=frequency,
                interval_days=resolved_interval_days,
            )
        except ReminderConfigurationError as error:
            raise BadRequestError(str(error)) from error

    async def _update_detached_task(
        self,
        user_id: int,
        task: Task,
        *,
        title: str | None,
        description: str | None | UnsetType,
        priority: Priority | None,
        due_at: datetime | None,
        project_id: int | None | UnsetType,
        label_ids: Sequence[int] | None,
        reminders_enabled: bool | None,
        reminders: Sequence[ReminderInput] | None,
        recurrence: TaskRecurrence | None,
        interval_days: int | None,
    ) -> TaskDetail:
        """
        Update a task without an active series, including one-off conversions.

        Purpose:
            Apply the one-off update and, when requested, convert the task into
            a daily or periodic series with a phase-preserving overdue shift.

        Args:
            user_id: Owner identifier.
            task: Task without an active series; a stale soft-deleted series
                reference is cleared before conversion or an explicit
                ``one_off`` toggle.
            title: New title, or ``None`` when unchanged.
            description: New description sentinel or value.
            priority: New priority, or ``None`` when unchanged.
            due_at: New due instant, or ``None`` when unchanged.
            project_id: New project sentinel or value.
            label_ids: New label selection, or ``None`` when unchanged.
            reminders_enabled: New master toggle, or ``None`` when unchanged.
            reminders: New reminder rules, or ``None`` when unchanged.
            recurrence: Target recurrence mode, or ``None`` when unchanged.
            interval_days: Requested periodic interval, or ``None`` when
                unchanged.

        Returns:
            TaskDetail: The updated task detail with its recurrence mode.

        Raises:
            BadRequestError: If an updated field is invalid or the
                recurrence/interval pair is incompatible.
            NotFoundError: If the project or a label does not exist for this
                user.

        Notes:
            - Other fields are applied before the series snapshot is taken.
            - A changed due time on an incomplete task clears a stale
              soft-deleted series reference before the edit, so the update
              cannot collide with the unique ``(series_id, due_at)`` slot of a
              historical sibling.
            - A completed task of a soft-deleted series keeps its historical
              ``series_id`` on a due-time change without explicit
              ``recurrence`` when no other non-deleted task of the series
              occupies the new slot; a slot collision clears the reference.
            - An explicit ``recurrence`` clears the stale reference as before,
              and a field-only edit keeps the historical link.
            - Regular conversion accepts only ``None`` or ``1`` and stores
              ``interval_days=1``; periodic conversion requires ``interval_days
              >= 2``.
            - The overdue shift uses the user's timezone and the final due local
              date as the anchor.
            - Persists the mutated task with ``save_task`` and flushes through
              the unit of work; does not commit.
        """
        if recurrence is None and interval_days is not None:
            raise BadRequestError(_INVALID_RECURRENCE_INTERVAL)

        if (
            recurrence is None
            and task.series_id is not None
            and due_at is not None
            and due_at != task.due_at
            and (
                task.completed_at is None
                or await self._series_repository.series_due_slot_exists(
                    task.series_id,
                    user_id,
                    due_at,
                    exclude_task_id=task.id,
                )
            )
        ):
            task.series_id = None

        if recurrence is TaskRecurrence.ONE_OFF and task.series_id is not None:
            task.series_id = None

        if recurrence in (TaskRecurrence.REGULAR, TaskRecurrence.PERIODIC):
            if recurrence is TaskRecurrence.REGULAR:
                frequency = SeriesFrequency.DAILY
                resolved_interval_days = 1
            else:
                if interval_days is None:
                    raise BadRequestError(_INVALID_RECURRENCE_INTERVAL)
                frequency = SeriesFrequency.PERIODIC
                resolved_interval_days = interval_days
            if task.series_id is not None:
                task.series_id = None
            now = self._clock()
            final_due_at = due_at if due_at is not None else task.due_at
            will_shift = (
                task.completed_at is None
                and final_due_at.tzinfo is not None
                and final_due_at.utcoffset() is not None
                and final_due_at <= now
            )
            await self._apply_task_update(
                user_id,
                task,
                title=title,
                description=description,
                priority=priority,
                due_at=due_at,
                project_id=project_id,
                label_ids=label_ids,
                reminders_enabled=reminders_enabled,
                reminders=reminders,
                defer_reminder_rebuild=will_shift,
            )
            if will_shift:
                zone = await self._references.user_timezone(user_id)
                await self._shift_overdue_task(
                    user_id,
                    task,
                    anchor=task.due_at.astimezone(zone).date(),
                    interval_days=resolved_interval_days,
                    timezone=zone,
                    reminders=reminders,
                )
            await self._repository.save_task(task)
            await self._series_operations.create_from_task(
                user_id,
                task,
                frequency=frequency,
                interval_days=resolved_interval_days,
            )
            await self._unit_of_work.flush()
            return await self._details.detail(user_id, task)

        await self._apply_task_update(
            user_id,
            task,
            title=title,
            description=description,
            priority=priority,
            due_at=due_at,
            project_id=project_id,
            label_ids=label_ids,
            reminders_enabled=reminders_enabled,
            reminders=reminders,
        )
        await self._repository.save_task(task)
        await self._unit_of_work.flush()
        return await self._details.detail(user_id, task)

    async def _apply_task_update(
        self,
        user_id: int,
        task: Task,
        *,
        title: str | None,
        description: str | None | UnsetType,
        priority: Priority | None,
        due_at: datetime | None,
        project_id: int | None | UnsetType,
        label_ids: Sequence[int] | None,
        reminders_enabled: bool | None,
        reminders: Sequence[ReminderInput] | None,
        defer_reminder_rebuild: bool = False,
    ) -> None:
        """
        Apply a one-off task update to the current instance.

        Purpose:
            Share field validation, label replacement, and reminder rebuilding
            between plain edits and recurrence conversions.

        Args:
            user_id: Owner identifier.
            task: Task to update in place.
            title: New title, or ``None`` when unchanged.
            description: New description sentinel or value.
            priority: New priority, or ``None`` when unchanged.
            due_at: New due instant, or ``None`` when unchanged.
            project_id: New project sentinel or value.
            label_ids: New label selection, or ``None`` when unchanged.
            reminders_enabled: New master toggle, or ``None`` when unchanged.
            reminders: New reminder rules, or ``None`` when unchanged.
            defer_reminder_rebuild: Whether the caller shifts the due time
                afterwards and will rebuild reminders from the final due.
                Defaults to ``False``.

        Returns:
            None: The changes stay in the current unit of work.

        Raises:
            BadRequestError: If the title, due time, project, or a reminder rule
                is invalid. When ``defer_reminder_rebuild`` is true, reminder
                validation moves to the caller's overdue shift and this method
                does not validate the supplied rules.
            NotFoundError: If the project or a label does not exist for this
                user.

        Notes:
            - Due-time, reminder, or re-enabled master-toggle changes rebuild
              the affected reminder rows from the current time.
            - Disabling the master toggle persists any supplied rules first,
              then cancels deliveries and clears slots.
            - A deferred rebuild skips reminder row deletion so supplied or
              previous rules survive until the overdue shift completes.
            - Does not save, flush, or commit; the caller owns persistence and
              the transaction flush.
        """
        if title is not None:
            task.title = normalize_task_title(title)
        if not isinstance(description, UnsetType):
            task.description = description
        if priority is not None:
            task.priority = priority.value
        due_changed = due_at is not None and due_at != task.due_at
        if due_at is not None:
            validate_task_due_at(due_at)
            task.due_at = due_at
        if not isinstance(project_id, UnsetType):
            await self._references.validate_project(user_id, project_id)
            task.project_id = project_id
        if label_ids is not None:
            owned_label_ids = await self._references.validate_labels(user_id, label_ids)
            await self._repository.replace_task_labels(
                task.id,
                user_id,
                owned_label_ids,
            )
        toggle_changed = (
            reminders_enabled is not None and reminders_enabled != task.reminders_enabled
        )
        if reminders_enabled is not None:
            task.reminders_enabled = reminders_enabled

        rebuild_reminders = (
            due_changed or reminders is not None or (toggle_changed and reminders_enabled)
        ) and not defer_reminder_rebuild
        if rebuild_reminders:
            await self._rebuild_task_reminders(
                user_id,
                task,
                reminders=reminders,
            )
        if toggle_changed and not reminders_enabled:
            await self._reminder_repository.cancel_task_reminders(user_id, task.id)

    async def complete(self, user_id: int, task_id: int) -> TaskDetail:
        """
        Mark a task as completed and stop its deliveries.

        Purpose:
            Record completion while letting an owning series continue.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            TaskDetail: The completed task detail.

        Raises:
            NotFoundError: If the task does not exist for this user.

        Notes:
            - Sets ``completed_at`` to the current instant and persists it with
              ``save_task``.
            - Clears reminder slots and cancels pending, failed, and sending
              deliveries for this task only.
            - Locks the task row and does not commit.
        """
        task = await self._require_task(user_id, task_id, for_update=True)
        task.completed_at = self._clock()
        await self._reminder_repository.cancel_task_reminders(user_id, task.id)
        await self._repository.save_task(task)
        await self._unit_of_work.flush()
        return await self._details.detail(user_id, task)

    async def uncomplete(self, user_id: int, task_id: int) -> TaskDetail:
        """
        Reopen a task, rebuild its reminders, and restore a deleted series.

        Purpose:
            Restore delivery after accidental or reversed completion without
            backfilling missed slots, while rejecting a completed task whose
            due moment has already arrived, regardless of its series.
            A completed task of a soft-deleted series gets a new active series
            with the same schedule.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            TaskDetail: The reopened task detail.

        Raises:
            BadRequestError: If the task was completed and its ``due_at`` is
                at or before the current instant; or, when a soft-deleted
                series is recreated, if its schedule is invalid, its start
                date or generation range is unsupported, the stored user
                timezone is invalid, or the task project is archived.
            NotFoundError: If the task does not exist for this user, its
                referenced series does not exist, or the recreated series
                project or label does not exist for this user.

        Notes:
            - Clears ``completed_at``, persists it with ``save_task``, and
              recomputes each reminder's next slot.
            - The guard compares ``due_at`` with the current instant: a due
              moment at or before now is rejected for every completed task,
              including instances of an active series; only a future due
              moment is reopened.
            - A completed task of a soft-deleted series is reattached through
              ``SeriesOperations.create_from_task`` to a new series that copies
              the old ``frequency`` and ``interval_days``; the new series has
              ``ends_on`` set to ``None``, does not duplicate the current due
              slot, and generates future instances by the normal series rules.
              The old soft-deleted series row remains deleted as history.
            - Series recreation still requires a future due moment.
            - A before-due moment already in the past stays disabled.
            - A task that was not completed skips the due-moment guard and
              series recreation.
            - For a series task the series row is locked before the task row;
              one-off tasks lock only the task row.
            - Does not commit.
        """
        task, series = await self._lock_update_target(
            user_id,
            task_id,
            include_deleted_series=True,
        )
        now = self._clock()
        was_completed = task.completed_at is not None
        if was_completed and task.due_at <= now:
            raise BadRequestError(
                _PAST_DUE_DATE,
                description=_PAST_DUE_DATE_DESCRIPTION,
            )
        task.completed_at = None
        for reminder in await self._reminder_repository.list_task_reminders(user_id, task.id):
            try:
                next_run_at = rebuild_reminder_slot(reminder, due_at=task.due_at, now=now)
            except ReminderConfigurationError as error:
                raise BadRequestError(str(error)) from error
            await self._reminder_repository.update_reminder_next_run_at(
                reminder,
                next_run_at,
            )
        await self._repository.save_task(task)
        if was_completed and series is not None and series.deleted_at is not None:
            await self._series_operations.create_from_task(
                user_id,
                task,
                frequency=SeriesFrequency(series.frequency),
                interval_days=series.interval_days,
            )
        await self._unit_of_work.flush()
        return await self._details.detail(user_id, task)

    async def delete(self, user_id: int, task_id: int) -> None:
        """
        Delete a task according to the recurrence-aware deletion matrix.

        Purpose:
            Remove the requested task while preserving history and, for an
            active series, stopping the whole schedule.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            None: The deletion stays in the current unit of work.

        Raises:
            NotFoundError: If the task or its referenced series does not exist
                for this user.

        Notes:
            - Any task of an active series delegates to
              ``SeriesOperations.delete`` with this task as the target,
              regardless of the task's completion state.
            - That call soft-deletes the series, physically deletes all future
              incomplete instances, and soft-deletes the selected completed or
              overdue task when it survives hard deletion.
            - A one-off task or a task whose series is soft-deleted is
              soft-deleted individually with its deliveries cancelled.
            - For a series task the series row is locked before the task row;
              one-off tasks lock only the task row.
            - Does not commit.
        """
        task, series = await self._lock_update_target(user_id, task_id)
        if series is not None:
            await self._series_operations.delete(
                user_id,
                series.id,
                target_task_id=task.id,
            )
            return
        now = self._clock()
        await self._reminder_repository.cancel_task_reminders(user_id, task.id)
        await self._repository.soft_delete_task(task, now, uuid4())
        await self._unit_of_work.flush()

    async def _lock_update_target(
        self,
        user_id: int,
        task_id: int,
        *,
        include_deleted_series: bool = False,
    ) -> tuple[Task, TaskSeries | None]:
        """
        Lock the update targets in the canonical series-before-task order.

        Purpose:
            Avoid a deadlock between series-wide updates that lock the series
            row before its instances and instance edits, deletes, or reopenings
            that would otherwise lock the task row first.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.
            include_deleted_series: When ``True``, return a soft-deleted series
                instead of collapsing it to ``None``, so the caller can recreate
                its schedule. Defaults to ``False``.

        Returns:
            tuple[Task, TaskSeries | None]: The locked task and its locked
                series, or ``None`` when the task is one-off or its soft-deleted
                series is omitted by ``include_deleted_series``.

        Raises:
            NotFoundError: If the task or a referenced series does not exist
                for this user.

        Notes:
            - Reads the task without a lock to discover its series, locks the
              series row even when soft-deleted, then locks the task row.
            - By default a soft-deleted series is returned as ``None`` so
              callers treat the task as one-off while its history keeps the
              series reference.
            - A missing series (integrity violation) still raises not-found.
            - One-off tasks lock only the task row.
            - Does not commit.
        """
        initial = await self._repository.get_task(user_id, task_id)
        if initial is None:
            raise NotFoundError(_TASK_NOT_FOUND)
        series: TaskSeries | None = None
        if initial.series_id is not None:
            series = await self._series_repository.get_series(
                user_id,
                initial.series_id,
                for_update=True,
                include_deleted=True,
            )
            if series is None:
                raise NotFoundError("Series not found")
            if series.deleted_at is not None and not include_deleted_series:
                series = None
        task = await self._repository.get_task(user_id, task_id, for_update=True)
        if task is None:
            raise NotFoundError(_TASK_NOT_FOUND)
        return task, series

    async def _update_series_task(
        self,
        user_id: int,
        task: Task,
        series: TaskSeries,
        *,
        title: str | None,
        description: str | None | UnsetType,
        priority: Priority | None,
        due_at: datetime | None,
        project_id: int | None | UnsetType,
        label_ids: Sequence[int] | None,
        reminders_enabled: bool | None,
        reminders: Sequence[ReminderInput] | None,
        frequency: SeriesFrequency | None = None,
        interval_days: int | None = None,
    ) -> TaskDetail:
        """
        Apply a task-card edit through the series rule.

        Purpose:
            Keep series semantics intact when the user edits any instance,
            including changing its frequency or periodic interval.

        Args:
            user_id: Owner identifier.
            task: Series instance being edited.
            series: Series row already locked by the caller.
            title: New title, or ``None`` when unchanged.
            description: New description sentinel or value.
            priority: New priority, or ``None`` when unchanged.
            due_at: New due instant used to derive a series wall-clock time.
            project_id: New project sentinel or value.
            label_ids: New label selection, or ``None`` when unchanged.
            reminders_enabled: New master toggle, or ``None`` when unchanged.
            reminders: New reminder rules, or ``None`` when unchanged.
            frequency: New series frequency, or ``None`` to keep it. Defaults
                to ``None``.
            interval_days: New number of days between periodic occurrences, or
                ``None`` to keep the current interval. Defaults to ``None``.

        Returns:
            TaskDetail: The updated representative instance detail.

        Raises:
            NotFoundError: If the series does not exist for this user.
            BadRequestError: If an updated field or the frequency/interval pair
                is invalid.

        Notes:
            - A changed due time moves the series ``time_of_day`` in the series
              timezone; individual detached instances are not supported.
            - A frequency or interval change regenerates future instances
              through the series operations.
            - The series row is locked before the instance row by the caller.
            - Does not commit.
        """
        time_of_day: time | None = None
        if due_at is not None:
            validate_task_due_at(due_at)
            local_due = due_at.astimezone(ZoneInfo(series.timezone))
            candidate = local_due.timetz().replace(tzinfo=None)
            if candidate != series.time_of_day:
                time_of_day = candidate
        await self._series_operations.update(
            user_id,
            series.id,
            title=title,
            description=description,
            priority=priority,
            frequency=frequency,
            interval_days=interval_days,
            time_of_day=time_of_day,
            project_id=project_id,
            label_ids=label_ids,
            reminders_enabled=reminders_enabled,
            reminders=reminders,
        )
        refreshed = await self._repository.get_task(user_id, task.id)
        if refreshed is None:
            future_tasks = await self._repository.list_future_series_tasks(
                series.id,
                user_id,
                self._clock(),
            )
            refreshed = future_tasks[0] if future_tasks else task
        return await self._details.detail(user_id, refreshed)

    async def _shift_overdue_task(
        self,
        user_id: int,
        task: Task,
        *,
        anchor: date,
        interval_days: int,
        timezone: ZoneInfo,
        reminders: Sequence[ReminderInput] | None = None,
    ) -> None:
        """
        Move an overdue incomplete task to the next phase-aligned slot.

        Purpose:
            Keep an overdue task actionable after a recurrence toggle without
            losing the original local time or series phase.

        Args:
            user_id: Owner identifier.
            task: Task to shift in place.
            anchor: First phase date used to align the next occurrence.
            interval_days: Number of local days between occurrences.
            timezone: Timezone used to derive the local date and time.
            reminders: Reminder rules from the same update, or ``None`` to
                rebuild from the task's previous rules. Defaults to ``None``.

        Returns:
            None: The changes stay in the current unit of work.

        Raises:
            BadRequestError: If rebuilding the task reminders fails validation.

        Notes:
            - No-op when the task is completed or its due time is still in the
              future.
            - Preserves the current local wall-clock time in ``timezone``.
            - Rebuilds the task reminder slots from the new due time, using the
              supplied rules when present.
            - Clears reminder slots again when the master toggle is disabled.
            - Does not commit.
        """
        now = self._clock()
        if task.completed_at is not None or task.due_at > now:
            return
        time_of_day = task.due_at.astimezone(timezone).timetz().replace(tzinfo=None)
        task.due_at = next_occurrence_after_date(
            anchor=anchor,
            interval_days=interval_days,
            time_of_day=time_of_day,
            timezone=timezone,
            now=now,
        )
        await self._rebuild_task_reminders(user_id, task, reminders=reminders)
        if not task.reminders_enabled:
            await self._reminder_repository.cancel_task_reminders(user_id, task.id)

    async def _rebuild_task_reminders(
        self,
        user_id: int,
        task: Task,
        *,
        reminders: Sequence[ReminderInput] | None,
    ) -> None:
        """
        Cancel, delete, and recreate task reminders from the current state.

        Purpose:
            Apply due-time and reminder-configuration changes without leaving
            stale slots or duplicate reminder kinds.

        Args:
            user_id: Owner identifier.
            task: Task whose reminders change.
            reminders: New reminder rules, or ``None`` to reuse persisted
                configurations.

        Returns:
            None: The changes stay in the current unit of work.

        Raises:
            BadRequestError: If a reminder rule is invalid.

        Notes:
            - Active deliveries are cancelled before the reminder rows are
              replaced.
            - Does not commit.
        """
        existing = await self._reminder_repository.list_task_reminders(user_id, task.id)
        if reminders is not None:
            try:
                configs = validate_reminder_inputs(reminders)
            except ReminderConfigurationError as error:
                raise BadRequestError(str(error)) from error
        else:
            configs = tuple(reminder_input_from_row(reminder) for reminder in existing)
        zone = await self._references.user_timezone(user_id)
        now = self._clock()
        await self._reminder_repository.cancel_task_reminders(user_id, task.id)
        await self._reminder_repository.delete_task_reminders(user_id, task.id)
        for config in configs:
            try:
                row = build_task_reminder(
                    config,
                    user_id=user_id,
                    task_id=task.id,
                    due_at=task.due_at,
                    timezone_name=zone.key,
                    now=now,
                )
            except ReminderConfigurationError as error:
                raise BadRequestError(str(error)) from error
            if row is not None:
                await self._reminder_repository.add_reminder(row)

    async def _require_task(
        self,
        user_id: int,
        task_id: int,
        *,
        for_update: bool = False,
    ) -> Task:
        """
        Load a task or raise a not-found error.

        Purpose:
            Treat missing tasks and another user's tasks identically.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Task: The matching non-deleted task.

        Raises:
            NotFoundError: If no task matches the user and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        task = await self._repository.get_task(
            user_id,
            task_id,
            for_update=for_update,
        )
        if task is None:
            raise NotFoundError(_TASK_NOT_FOUND)
        return task

    def _local_day_bounds(
        self,
        zone: ZoneInfo,
        local_date: date,
    ) -> tuple[datetime, datetime]:
        """
        Convert a local calendar date into UTC day bounds.

        Purpose:
            Share the user's local-day window between task listing and the
            "today" progress summary.

        Args:
            zone: User's timezone used to localize midnight.
            local_date: Local calendar date.

        Returns:
            tuple[datetime, datetime]: The inclusive start instant and the
                exclusive end instant of the local day, expressed in UTC.

        Raises:
            BadRequestError: If either local midnight overflows the supported
                calendar range.

        Notes:
            - Both bounds resolve local midnight in ``zone``, so daylight-saving
              transitions keep a 23- or 25-hour local day.
            - Performs no writes and does not commit.
        """
        try:
            start = localize_wall_time(local_date, time(0, 0), zone)
            end = localize_wall_time(local_date + timedelta(days=1), time(0, 0), zone)
        except OverflowError as error:
            raise BadRequestError(_INVALID_DATE_RANGE) from error
        return start, end
