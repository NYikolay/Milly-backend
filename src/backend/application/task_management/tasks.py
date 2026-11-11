from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from backend.application.ports.collaborators import ProjectReader
from backend.application.ports.task_management import ReminderRepository, TaskRepository
from backend.application.ports.unit_of_work import UnitOfWork
from backend.application.task_management.task_details import TaskDetails
from backend.application.task_management.task_inputs import (
    normalize_task_title,
    validate_task_due_at,
)
from backend.application.task_management.task_references import TaskReferences
from backend.core.errors import BadRequestError, NotFoundError
from backend.domain.entities import Task
from backend.domain.enums import Priority
from backend.domain.task_management.errors import ReminderConfigurationError
from backend.domain.task_management.models import (
    ReminderInput,
    TaskDetail,
    TaskFilters,
    TaskListCursor,
    TaskPage,
)
from backend.domain.task_management.policies import (
    build_task_reminder,
    localize_wall_time,
    validate_reminder_inputs,
)

_TASK_NOT_FOUND = "Task not found"
_INVALID_DATE_RANGE = "Date range is out of bounds"

__all__ = ["TaskDetail", "TaskFilters", "TaskListCursor", "TaskPage", "TaskService"]


class TaskService:
    """Coordinates task queries, one-off creation, and completion."""

    def __init__(
        self,
        repository: TaskRepository,
        reminder_repository: ReminderRepository,
        project_reader: ProjectReader,
        references: TaskReferences,
        details: TaskDetails,
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
            reminder_repository: Persistence operations for task reminders.
            project_reader: Project ownership and lifecycle validation plus the
                today progress aggregate.
            references: Project, label, and timezone reference validation.
            details: Shared task-detail assembly.
            unit_of_work: Transaction boundary owned by the caller.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository
        self._reminder_repository = reminder_repository
        self._project_reader = project_reader
        self._references = references
        self._details = details
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
