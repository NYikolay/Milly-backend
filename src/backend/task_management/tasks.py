from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.core.errors import BadRequestError, NotFoundError
from backend.models.label import Label
from backend.models.project import Project
from backend.models.task import Task
from backend.models.task_reminder import TaskReminder
from backend.models.task_series import TaskSeries
from backend.task_management.enums import Priority
from backend.task_management.ports import TaskManagementRepository
from backend.task_management.reminders import (
    ReminderInput,
    build_task_reminder,
    rebuild_reminder_slot,
    reminder_input_from_row,
    validate_reminder_inputs,
)
from backend.task_management.schedule import localize_wall_time
from backend.task_management.series import UNSET, SeriesService, UnsetType

_TASK_NOT_FOUND = "Task not found"
_LABEL_NOT_FOUND = "Label not found"
_TITLE_REQUIRED = "Task title is required"
_INVALID_DUE_AT = "Task due time must include a timezone"
_INVALID_TIMEZONE = "Invalid timezone"


@dataclass(frozen=True)
class TaskFilters:
    """Filters applied when listing tasks."""

    date: date | None = None
    project_ids: tuple[int, ...] = ()
    priorities: tuple[Priority, ...] = ()
    status: Literal["all", "open", "completed", "overdue"] = "all"
    recurrence: Literal["all", "regular", "one_off"] = "all"
    query: str | None = None
    include_archived_projects: bool = False


@dataclass(frozen=True)
class TaskDetail:
    """A task with its labels, reminders, and owning project."""

    task: Task
    labels: Sequence[Label]
    reminders: Sequence[TaskReminder]
    project: Project | None


class TaskService:
    """Coordinates one-off tasks and series-instance operations."""

    def __init__(
        self,
        repository: TaskManagementRepository,
        series_service: SeriesService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with persistence, series rules, and time ports.

        Purpose:
            Keep task rules independent of request handling and make
            timestamps deterministic through an injectable clock.

        Args:
            repository: Persistence operations for tasks, labels, reminders,
                and projects.
            series_service: Applied when a task belongs to a recurring series.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository
        self._series_service = series_service
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def list(self, user_id: int, filters: TaskFilters) -> list[TaskDetail]:
        """
        List task details for a user.

        Purpose:
            Provide the filtered task list with labels, reminders, and project
            data.

        Args:
            user_id: Owner identifier.
            filters: Date, project, priority, status, recurrence, text, and
                archive filters.

        Returns:
            list[TaskDetail]: Matching task details in repository order.

        Raises:
            BadRequestError: If the stored user timezone is invalid.

        Notes:
            - The date filter uses UTC bounds of the user's local day.
            - Performs no writes and does not commit.
        """
        date_start: datetime | None = None
        date_end: datetime | None = None
        if filters.date is not None:
            zone = await self._user_timezone(user_id)
            date_start = localize_wall_time(filters.date, time(0, 0), zone)
            date_end = localize_wall_time(
                filters.date + timedelta(days=1),
                time(0, 0),
                zone,
            )
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
        )
        return [await self._detail(user_id, task) for task in tasks]

    async def get(self, user_id: int, task_id: int) -> TaskDetail:
        """
        Load one owned task detail.

        Purpose:
            Resolve a task for a detail response.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            TaskDetail: The matching task with labels, reminders, and project.

        Raises:
            NotFoundError: If the task does not exist for this user.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._detail(user_id, await self._require_task(user_id, task_id))

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
            BadRequestError: If the title is empty, the due time is naive, the
                timezone is invalid, or a reminder rule is invalid.
            NotFoundError: If the project or a label does not exist for this
                user.

        Notes:
            - Past before-due moments are skipped.
            - Scheduled reminders start from the first local slot on or after
              the due date and always land in the future.
            - Flushes through the repository but does not commit.
        """
        normalized_title = self._normalize_title(title)
        self._validate_due_at(due_at)
        await self._validate_project(user_id, project_id)
        owned_label_ids = await self._validate_labels(user_id, label_ids)
        validated_reminders = validate_reminder_inputs(reminders)
        zone = await self._user_timezone(user_id)
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
        await self._repository.add_task(task)
        await self._repository.add_task_labels(task.id, user_id, owned_label_ids)
        for reminder in validated_reminders:
            row = build_task_reminder(
                reminder,
                user_id=user_id,
                task_id=task.id,
                due_at=due_at,
                timezone_name=zone.key,
                now=now,
            )
            if row is not None:
                await self._repository.add_reminder(row)
        await self._repository.flush()
        return await self._detail(user_id, task)

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
    ) -> TaskDetail:
        """
        Update a task or its recurring series.

        Purpose:
            Apply snapshot, label, due-time, and reminder changes; a task that
            belongs to a series is edited through the series rule.

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

        Returns:
            TaskDetail: The updated task detail.

        Raises:
            NotFoundError: If the task, project, label, or series does not
                exist for this user.
            BadRequestError: If the title is empty, the due time is naive, or a
                reminder rule is invalid.

        Notes:
            - For a series instance only future incomplete instances change.
            - Due-time, reminder, or re-enabled master-toggle changes rebuild
              the affected reminder rows from the current time.
            - Disabling the master toggle persists any supplied rules first,
              then cancels deliveries and clears slots.
            - For a series instance the series row is locked before the task
              row; one-off tasks lock only the task row.
            - Does not commit.
        """
        task, series = await self._lock_update_target(user_id, task_id)
        if series is not None:
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
            )

        if title is not None:
            task.title = self._normalize_title(title)
        if not isinstance(description, UnsetType):
            task.description = description
        if priority is not None:
            task.priority = priority.value
        due_changed = due_at is not None and due_at != task.due_at
        if due_at is not None:
            self._validate_due_at(due_at)
            task.due_at = due_at
        if not isinstance(project_id, UnsetType):
            await self._validate_project(user_id, project_id)
            task.project_id = project_id
        if label_ids is not None:
            owned_label_ids = await self._validate_labels(user_id, label_ids)
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
        )
        if rebuild_reminders:
            await self._rebuild_task_reminders(
                user_id,
                task,
                reminders=reminders,
            )
        if toggle_changed and not reminders_enabled:
            await self._repository.cancel_task_reminders(user_id, task.id)
        await self._repository.flush()
        return await self._detail(user_id, task)

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
            - Sets ``completed_at`` to the current instant.
            - Clears reminder slots and cancels pending, failed, and sending
              deliveries for this task only.
            - Locks the task row and does not commit.
        """
        task = await self._require_task(user_id, task_id, for_update=True)
        task.completed_at = self._clock()
        await self._repository.cancel_task_reminders(user_id, task.id)
        await self._repository.flush()
        return await self._detail(user_id, task)

    async def uncomplete(self, user_id: int, task_id: int) -> TaskDetail:
        """
        Reopen a task and rebuild its reminders from the current time.

        Purpose:
            Restore delivery after accidental or reversed completion without
            backfilling missed slots.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            TaskDetail: The reopened task detail.

        Raises:
            NotFoundError: If the task does not exist for this user.

        Notes:
            - Clears ``completed_at`` and recomputes each reminder's next slot.
            - A before-due moment already in the past stays disabled.
            - Locks the task row and does not commit.
        """
        task = await self._require_task(user_id, task_id, for_update=True)
        task.completed_at = None
        now = self._clock()
        for reminder in await self._repository.list_task_reminders(user_id, task.id):
            await self._repository.update_reminder_next_run_at(
                reminder,
                rebuild_reminder_slot(reminder, due_at=task.due_at, now=now),
            )
        await self._repository.flush()
        return await self._detail(user_id, task)

    async def delete(self, user_id: int, task_id: int) -> None:
        """
        Delete a task or its recurring series.

        Purpose:
            Remove a one-off task or apply the series deletion rule for an
            instance.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            None: The deletion stays in the current unit of work.

        Raises:
            NotFoundError: If the task does not exist for this user.

        Notes:
            - A one-off task is soft-deleted and its deliveries are cancelled.
            - A series instance delegates to ``SeriesService.delete``.
            - For a series instance the series row is locked before the task
              row; one-off tasks lock only the task row.
            - Does not commit.
        """
        task, series = await self._lock_update_target(user_id, task_id)
        if series is not None:
            await self._series_service.delete(user_id, series.id)
            return
        await self._repository.cancel_task_reminders(user_id, task.id)
        await self._repository.soft_delete_task(task, self._clock(), uuid4())
        await self._repository.flush()

    async def _lock_update_target(
        self,
        user_id: int,
        task_id: int,
    ) -> tuple[Task, TaskSeries | None]:
        """
        Lock the update targets in the canonical series-before-task order.

        Purpose:
            Avoid a deadlock between series-wide updates that lock the series
            row before its instances and instance edits or deletes that would
            otherwise lock the task row first.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            tuple[Task, TaskSeries | None]: The locked task and its locked
                series when the task belongs to one.

        Raises:
            NotFoundError: If the task or its series does not exist for this
                user.

        Notes:
            - Reads the task without a lock to discover its series, locks the
              series row, then locks the task row.
            - One-off tasks lock only the task row.
            - Does not commit.
        """
        initial = await self._repository.get_task(user_id, task_id)
        if initial is None:
            raise NotFoundError(_TASK_NOT_FOUND)
        series: TaskSeries | None = None
        if initial.series_id is not None:
            series = await self._repository.get_series(
                user_id,
                initial.series_id,
                for_update=True,
            )
            if series is None:
                raise NotFoundError("Series not found")
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
    ) -> TaskDetail:
        """
        Apply a task-card edit through the series rule.

        Purpose:
            Keep series semantics intact when the user edits any instance.

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

        Returns:
            TaskDetail: The updated representative instance detail.

        Raises:
            NotFoundError: If the series does not exist for this user.
            BadRequestError: If an updated field is invalid.

        Notes:
            - A changed due time moves the series ``time_of_day`` in the series
              timezone; individual detached instances are not supported.
            - The series row is locked before the instance row by the caller.
            - Does not commit.
        """
        time_of_day: time | None = None
        if due_at is not None:
            self._validate_due_at(due_at)
            local_due = due_at.astimezone(ZoneInfo(series.timezone))
            candidate = local_due.timetz().replace(tzinfo=None)
            if candidate != series.time_of_day:
                time_of_day = candidate
        await self._series_service.update(
            user_id,
            series.id,
            title=title,
            description=description,
            priority=priority,
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
        return await self._detail(user_id, refreshed)

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
        existing = await self._repository.list_task_reminders(user_id, task.id)
        configs = (
            validate_reminder_inputs(reminders)
            if reminders is not None
            else tuple(reminder_input_from_row(reminder) for reminder in existing)
        )
        zone = await self._user_timezone(user_id)
        now = self._clock()
        await self._repository.cancel_task_reminders(user_id, task.id)
        await self._repository.delete_task_reminders(user_id, task.id)
        for config in configs:
            row = build_task_reminder(
                config,
                user_id=user_id,
                task_id=task.id,
                due_at=task.due_at,
                timezone_name=zone.key,
                now=now,
            )
            if row is not None:
                await self._repository.add_reminder(row)

    async def _detail(self, user_id: int, task: Task) -> TaskDetail:
        """
        Load the labels, reminders, and project of a task.

        Purpose:
            Build the shared task-detail response shape.

        Args:
            user_id: Owner identifier.
            task: Task to enrich.

        Returns:
            TaskDetail: The task with its related data.

        Notes:
            - Missing or archived projects still resolve as ``None`` only when
              the repository hides them; existing projects are returned.
            - Performs no writes and does not commit.
        """
        labels = await self._repository.list_task_labels(user_id, task.id)
        reminders = await self._repository.list_task_reminders(user_id, task.id)
        project = (
            await self._repository.get_project(user_id, task.project_id)
            if task.project_id is not None
            else None
        )
        return TaskDetail(
            task=task,
            labels=labels,
            reminders=reminders,
            project=project,
        )

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

    async def _validate_project(self, user_id: int, project_id: int | None) -> None:
        """
        Validate an optional project reference.

        Purpose:
            Reject missing, cross-user, or deleted projects before writing.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier or ``None``.

        Returns:
            None: The project is valid or absent.

        Raises:
            NotFoundError: If the project does not exist for this user.
        """
        if project_id is None:
            return
        project = await self._repository.get_project(user_id, project_id)
        if project is None:
            raise NotFoundError("Project not found")

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
        """
        unique_ids = tuple(dict.fromkeys(label_ids))
        labels = await self._repository.get_labels_by_ids(user_id, unique_ids)
        if len(labels) != len(unique_ids):
            raise NotFoundError(_LABEL_NOT_FOUND)
        return unique_ids

    async def _user_timezone(self, user_id: int) -> ZoneInfo:
        """
        Resolve the user's IANA timezone.

        Purpose:
            Compute local task-day bounds and scheduled reminder slots.

        Args:
            user_id: Owner identifier.

        Returns:
            ZoneInfo: The user's timezone, defaulting to UTC when unset.

        Raises:
            BadRequestError: If the stored timezone is invalid.
        """
        name = await self._repository.get_user_timezone(user_id)
        try:
            return ZoneInfo(name or "UTC")
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise BadRequestError(_INVALID_TIMEZONE) from error

    def _normalize_title(self, title: str) -> str:
        """
        Trim a task title and reject an empty result.

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

    def _validate_due_at(self, due_at: datetime) -> None:
        """
        Require a timezone-aware due instant.

        Purpose:
            Keep stored due times as unambiguous absolute instants.

        Args:
            due_at: Candidate due instant.

        Returns:
            None: The due instant is aware.

        Raises:
            BadRequestError: If the due instant is naive.
        """
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise BadRequestError(_INVALID_DUE_AT)
