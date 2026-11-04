from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from backend.models.color_palette import ColorPalette
from backend.models.label import Label
from backend.models.project import Project
from backend.models.reminder_delivery import ReminderDelivery
from backend.models.task import Task
from backend.models.task_label import TaskLabel
from backend.models.task_reminder import TaskReminder
from backend.models.task_series import TaskSeries
from backend.task_management.enums import DeliveryStatus, ReminderKind
from backend.task_management.reminders import rebuild_reminder_slot
from backend.task_management.schedule import HORIZON_DAYS

_ACTIVE_DELIVERY_STATUSES = {
    DeliveryStatus.PENDING.value,
    DeliveryStatus.FAILED.value,
    DeliveryStatus.SENDING.value,
}


class FakeSender:
    """Telegram sender double that records messages and can fail on demand."""

    def __init__(self, *, message_id: int | None = 1, error: Exception | None = None) -> None:
        """
        Configure the sender double.

        Purpose:
            Let dispatcher tests assert the outgoing text and simulate Telegram
            failures.

        Args:
            message_id: Identifier returned by successful sends.
            error: Exception raised by every send when provided.
        """
        self.message_id = message_id
        self.error = error
        self.calls: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str) -> int | None:
        """
        Record a send and optionally raise a configured error.

        Purpose:
            Mirror the Telegram sender contract used by the dispatcher.

        Args:
            chat_id: Telegram chat identifier.
            text: Message text.

        Returns:
            int | None: Configured Telegram message identifier.

        Raises:
            Exception: The configured error when ``error`` is set.
        """
        self.calls.append((chat_id, text))
        if self.error is not None:
            raise self.error
        return self.message_id


class FakeTaskManagementRepository:
    """In-memory repository implementing the task-management persistence contract."""

    def __init__(self) -> None:
        """Initialize empty collections, counters, and a default palette color."""
        self.user_timezones: dict[int, str] = {}
        self.telegram_ids: dict[int, int] = {}
        self.colors: dict[int, ColorPalette] = {
            1: ColorPalette(
                id=1,
                slug="default",
                name="Default",
                hex="#000000",
                sort_order=1,
                is_active=True,
            )
        }
        self.projects: dict[int, Project] = {}
        self.labels: dict[int, Label] = {}
        self.tasks: dict[int, Task] = {}
        self.series: dict[int, TaskSeries] = {}
        self.reminders: dict[int, TaskReminder] = {}
        self.deliveries: dict[int, ReminderDelivery] = {}
        self.task_labels: set[tuple[int, int]] = set()
        self.series_labels: set[tuple[int, int]] = set()
        self.batch_calls: list[tuple[str, tuple[int, ...]]] = []
        self.bulk_calls: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.claimed_series_ids: list[int] = []
        self.blocked_series_ids: set[int] = set()
        self.series_errors: dict[int, Exception] = {}
        self.task_errors: set[int] = set()
        self.on_claim: Callable[[], None] | None = None
        self._next_ids: dict[str, int] = {}

    def _next_id(self, name: str, collection: dict[int, Any]) -> int:
        """Return the next identifier for a collection."""
        next_id = max(collection, default=0) + 1
        self._next_ids[name] = next_id
        return next_id

    def insert_color(self, color: ColorPalette) -> ColorPalette:
        """Insert a palette color and return it."""
        if color.id is None:
            color.id = self._next_id("color", self.colors)
        self.colors[color.id] = color
        return color

    def insert_project(self, project: Project) -> Project:
        """Insert a project and return it."""
        if project.id is None:
            project.id = self._next_id("project", self.projects)
        self.projects[project.id] = project
        return project

    def insert_label(self, label: Label) -> Label:
        """Insert a label and return it."""
        if label.id is None:
            label.id = self._next_id("label", self.labels)
        self.labels[label.id] = label
        return label

    def insert_task(self, task: Task) -> Task:
        """Insert a task and return it."""
        if task.id is None:
            task.id = self._next_id("task", self.tasks)
        self.tasks[task.id] = task
        return task

    def insert_series(self, series: TaskSeries) -> TaskSeries:
        """Insert a series and return it."""
        if series.id is None:
            series.id = self._next_id("series", self.series)
        self.series[series.id] = series
        return series

    def insert_reminder(self, reminder: TaskReminder) -> TaskReminder:
        """Insert a reminder and return it."""
        if reminder.id is None:
            reminder.id = self._next_id("reminder", self.reminders)
        self.reminders[reminder.id] = reminder
        return reminder

    def insert_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        """Insert a delivery and return it."""
        if delivery.id is None:
            delivery.id = self._next_id("delivery", self.deliveries)
        self.deliveries[delivery.id] = delivery
        return delivery

    def _project_is_active(self, project_id: int | None) -> bool:
        """Return whether an optional project reference is active."""
        if project_id is None:
            return True
        project = self.projects.get(project_id)
        return project is not None and project.deleted_at is None and project.archived_at is None

    def _cancel_deliveries(self, reminder_ids: set[int]) -> None:
        """Cancel active deliveries for the selected reminders."""
        for delivery in self.deliveries.values():
            if (
                delivery.reminder_id in reminder_ids
                and delivery.status in _ACTIVE_DELIVERY_STATUSES
            ):
                delivery.status = DeliveryStatus.CANCELLED.value
                delivery.locked_at = None
                delivery.next_attempt_at = None

    async def list_palette_colors(self) -> Sequence[ColorPalette]:
        """Return every palette color ordered by display position."""
        return sorted(self.colors.values(), key=lambda color: (color.sort_order, color.id))

    async def list_active_palette_colors(self) -> Sequence[ColorPalette]:
        """Return active palette colors ordered by display position."""
        return [color for color in await self.list_palette_colors() if color.is_active]

    async def get_palette_color(self, color_id: int) -> ColorPalette | None:
        """Return one palette color."""
        return self.colors.get(color_id)

    async def get_palette_colors_by_ids(
        self,
        color_ids: Sequence[int],
    ) -> dict[int, ColorPalette]:
        """Return palette colors keyed by identifier and record the batch call."""
        self.batch_calls.append(("get_palette_colors_by_ids", tuple(color_ids)))
        return {
            color_id: self.colors[color_id] for color_id in color_ids if color_id in self.colors
        }

    async def get_default_palette_color(self) -> ColorPalette | None:
        """Return the first active palette color."""
        colors = await self.list_active_palette_colors()
        return colors[0] if colors else None

    async def count_active_palette_colors(self) -> int:
        """Count active palette colors."""
        return sum(1 for color in self.colors.values() if color.is_active)

    async def palette_color_is_used(self, color_id: int) -> bool:
        """Return whether a project or label references a color."""
        return any(project.color_id == color_id for project in self.projects.values()) or any(
            label.color_id == color_id for label in self.labels.values()
        )

    async def get_user_timezone(self, user_id: int) -> str | None:
        """Return the stored timezone for a user."""
        return self.user_timezones.get(user_id)

    async def get_telegram_id(self, user_id: int) -> int | None:
        """Return the linked Telegram chat identifier for a user."""
        return self.telegram_ids.get(user_id)

    async def list_projects(
        self,
        user_id: int,
        *,
        include_archived: bool,
        limit: int,
        after_archived: bool | None,
        after_name: str | None,
        after_project_id: int | None,
    ) -> Sequence[Project]:
        """List one cursor-bounded page of non-deleted projects owned by a user."""
        projects = sorted(
            (
                project
                for project in self.projects.values()
                if project.user_id == user_id
                and project.deleted_at is None
                and (include_archived or project.archived_at is None)
            ),
            key=lambda project: (
                project.archived_at is not None,
                project.name.lower(),
                project.id,
            ),
        )
        if after_archived is not None and after_name is not None and after_project_id is not None:
            projects = [
                project
                for project in projects
                if (
                    project.archived_at is not None,
                    project.name.lower(),
                    project.id,
                )
                > (after_archived, after_name, after_project_id)
            ]
        return projects[:limit]

    async def get_project(
        self,
        user_id: int,
        project_id: int,
        *,
        for_update: bool = False,
    ) -> Project | None:
        """Return a non-deleted project owned by a user."""
        project = self.projects.get(project_id)
        if project is None or project.user_id != user_id or project.deleted_at is not None:
            return None
        return project

    async def get_projects_by_ids(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, Project]:
        """Return non-deleted projects owned by a user keyed by identifier."""
        self.batch_calls.append(("get_projects_by_ids", tuple(project_ids)))
        projects = [await self.get_project(user_id, project_id) for project_id in project_ids]
        return {project.id: project for project in projects if project is not None}

    async def project_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_project_id: int | None = None,
    ) -> bool:
        """Return whether another active project uses the name."""
        return any(
            project.user_id == user_id
            and project.deleted_at is None
            and project.archived_at is None
            and project.id != exclude_project_id
            and project.name.lower() == name.lower()
            for project in self.projects.values()
        )

    async def add_project(self, project: Project) -> Project:
        """Persist a project."""
        return self.insert_project(project)

    async def count_projects(self, user_id: int, *, archived: bool) -> int:
        """Count non-deleted projects in one archive state."""
        return sum(
            1
            for project in self.projects.values()
            if project.user_id == user_id
            and project.deleted_at is None
            and ((project.archived_at is not None) == archived)
        )

    async def project_progress(self, user_id: int, project_id: int) -> tuple[int, int]:
        """Count all and completed tasks of a project."""
        tasks = [
            task
            for task in self.tasks.values()
            if task.user_id == user_id and task.project_id == project_id and task.deleted_at is None
        ]
        return len(tasks), sum(1 for task in tasks if task.completed_at is not None)

    async def project_progress_for_projects(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, tuple[int, int]]:
        """Count all and completed tasks grouped by project and record the batch call."""
        self.batch_calls.append(("project_progress_for_projects", tuple(project_ids)))
        progress: dict[int, tuple[int, int]] = {}
        for project_id in project_ids:
            tasks = [
                task
                for task in self.tasks.values()
                if task.user_id == user_id
                and task.project_id == project_id
                and task.deleted_at is None
            ]
            if tasks:
                progress[project_id] = (
                    len(tasks),
                    sum(1 for task in tasks if task.completed_at is not None),
                )
        return progress

    async def today_progress(
        self,
        user_id: int,
        start: datetime,
        end: datetime,
    ) -> tuple[int, int]:
        """Count all and completed tasks due within a window."""
        tasks = [
            task
            for task in self.tasks.values()
            if task.user_id == user_id and task.deleted_at is None and start <= task.due_at < end
        ]
        return len(tasks), sum(1 for task in tasks if task.completed_at is not None)

    async def soft_delete_project(
        self,
        project: Project,
        batch_id: UUID,
        deleted_at: datetime,
    ) -> None:
        """Soft-delete a project, its series, tasks, and active deliveries."""
        project.deleted_at = deleted_at
        project.deletion_batch_id = batch_id
        series_ids: set[int] = set()
        task_ids: set[int] = set()
        for series in self.series.values():
            if series.project_id == project.id and series.user_id == project.user_id:
                series.deleted_at = deleted_at
                series.deletion_batch_id = batch_id
                series_ids.add(series.id)
        for task in self.tasks.values():
            if task.project_id == project.id and task.user_id == project.user_id:
                task.deleted_at = deleted_at
                task.deletion_batch_id = batch_id
                task_ids.add(task.id)
        reminder_ids = {
            reminder.id
            for reminder in self.reminders.values()
            if reminder.user_id == project.user_id
            and (reminder.task_id in task_ids or reminder.series_id in series_ids)
        }
        for reminder_id in reminder_ids:
            self.reminders[reminder_id].next_run_at = None
        self._cancel_deliveries(reminder_ids)

    async def list_labels(self, user_id: int) -> Sequence[Label]:
        """List non-deleted labels owned by a user."""
        return sorted(
            (
                label
                for label in self.labels.values()
                if label.user_id == user_id and label.deleted_at is None
            ),
            key=lambda label: (label.name.lower(), label.id),
        )

    async def get_label(
        self,
        user_id: int,
        label_id: int,
        *,
        for_update: bool = False,
    ) -> Label | None:
        """Return a non-deleted label owned by a user."""
        label = self.labels.get(label_id)
        if label is None or label.user_id != user_id or label.deleted_at is not None:
            return None
        return label

    async def get_labels_by_ids(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> Sequence[Label]:
        """Return matching non-deleted owned labels."""
        return [
            self.labels[label_id]
            for label_id in label_ids
            if label_id in self.labels
            and self.labels[label_id].user_id == user_id
            and self.labels[label_id].deleted_at is None
        ]

    async def label_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_label_id: int | None = None,
    ) -> bool:
        """Return whether another non-deleted label uses the name."""
        return any(
            label.user_id == user_id
            and label.deleted_at is None
            and label.id != exclude_label_id
            and label.name.lower() == name.lower()
            for label in self.labels.values()
        )

    async def add_label(self, label: Label) -> Label:
        """Persist a label."""
        return self.insert_label(label)

    async def soft_delete_label(self, label: Label, deleted_at: datetime) -> None:
        """Soft-delete a label."""
        label.deleted_at = deleted_at

    async def planned_tasks_count(self, user_id: int) -> int:
        """Count incomplete tasks in active plans."""
        return sum(
            1
            for task in self.tasks.values()
            if task.user_id == user_id
            and task.deleted_at is None
            and task.completed_at is None
            and self._project_is_active(task.project_id)
        )

    async def list_tasks(
        self,
        user_id: int,
        *,
        date_start: datetime | None,
        date_end: datetime | None,
        project_ids: Sequence[int],
        priorities: Sequence[str],
        status: str,
        recurrence: str,
        query: str | None,
        include_archived_projects: bool,
        now: datetime,
        limit: int,
        after_due_at: datetime | None,
        after_task_id: int | None,
    ) -> Sequence[Task]:
        """List tasks applying service filters and the cursor page bounds."""
        tasks = [
            task
            for task in self.tasks.values()
            if task.user_id == user_id and task.deleted_at is None
        ]
        if date_start is not None:
            tasks = [task for task in tasks if task.due_at >= date_start]
        if date_end is not None:
            tasks = [task for task in tasks if task.due_at < date_end]
        if project_ids:
            tasks = [task for task in tasks if task.project_id in project_ids]
        if priorities:
            tasks = [task for task in tasks if str(task.priority) in priorities]
        if status == "open":
            tasks = [task for task in tasks if task.completed_at is None]
        elif status == "completed":
            tasks = [task for task in tasks if task.completed_at is not None]
        elif status == "overdue":
            tasks = [task for task in tasks if task.completed_at is None and task.due_at < now]
        if recurrence == "regular":
            tasks = [task for task in tasks if task.series_id is not None]
        elif recurrence == "one_off":
            tasks = [task for task in tasks if task.series_id is None]
        if query:
            lowered = query.lower()
            tasks = [
                task
                for task in tasks
                if lowered in task.title.lower() or lowered in (task.description or "").lower()
            ]
        if not include_archived_projects:
            tasks = [task for task in tasks if self._project_is_active(task.project_id)]
        if after_due_at is not None and after_task_id is not None:
            tasks = [
                task
                for task in tasks
                if task.due_at > after_due_at
                or (task.due_at == after_due_at and task.id > after_task_id)
            ]
        return sorted(tasks, key=lambda task: (task.due_at, task.id))[:limit]

    async def get_task(
        self,
        user_id: int,
        task_id: int,
        *,
        for_update: bool = False,
    ) -> Task | None:
        """Return a non-deleted task owned by a user."""
        if task_id in self.task_errors:
            raise RuntimeError("task lookup failed")
        task = self.tasks.get(task_id)
        if task is None or task.user_id != user_id or task.deleted_at is not None:
            return None
        return task

    async def add_task(self, task: Task) -> Task:
        """Persist a task."""
        return self.insert_task(task)

    async def add_tasks(self, tasks: Sequence[Task]) -> Sequence[Task]:
        """Persist multiple tasks in one batch."""
        self.bulk_calls.append("add_tasks")
        return [self.insert_task(task) for task in tasks]

    async def soft_delete_task(
        self,
        task: Task,
        deleted_at: datetime,
        batch_id: UUID | None = None,
    ) -> None:
        """Soft-delete a task."""
        task.deleted_at = deleted_at
        task.deletion_batch_id = batch_id

    async def add_task_labels(
        self,
        task_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """Assign labels to a task."""
        for label_id in label_ids:
            self.task_labels.add((task_id, label_id))

    async def add_task_label_rows(self, rows: Sequence[TaskLabel]) -> None:
        """Assign multiple task-label rows in one batch."""
        self.bulk_calls.append("add_task_label_rows")
        for row in rows:
            self.task_labels.add((row.task_id, row.label_id))

    async def replace_task_labels(
        self,
        task_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """Replace all labels assigned to a task."""
        self.task_labels = {pair for pair in self.task_labels if pair[0] != task_id}
        await self.add_task_labels(task_id, user_id, label_ids)

    async def list_task_labels(self, user_id: int, task_id: int) -> Sequence[Label]:
        """Return labels assigned to a task."""
        return [
            self.labels[label_id]
            for pair_task_id, label_id in sorted(self.task_labels)
            if pair_task_id == task_id
            and label_id in self.labels
            and self.labels[label_id].user_id == user_id
            and self.labels[label_id].deleted_at is None
        ]

    async def list_task_labels_for_tasks(
        self,
        user_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Sequence[Label]]:
        """Return labels assigned to each requested task."""
        self.batch_calls.append(("list_task_labels_for_tasks", tuple(task_ids)))
        return {task_id: await self.list_task_labels(user_id, task_id) for task_id in task_ids}

    async def list_task_reminders(
        self,
        user_id: int,
        task_id: int,
    ) -> Sequence[TaskReminder]:
        """Return reminders attached to a task."""
        return sorted(
            (
                reminder
                for reminder in self.reminders.values()
                if reminder.user_id == user_id and reminder.task_id == task_id
            ),
            key=lambda reminder: reminder.id,
        )

    async def list_task_reminders_for_tasks(
        self,
        user_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Sequence[TaskReminder]]:
        """Return reminders attached to each requested task."""
        self.batch_calls.append(("list_task_reminders_for_tasks", tuple(task_ids)))
        return {task_id: await self.list_task_reminders(user_id, task_id) for task_id in task_ids}

    async def list_series_reminders(
        self,
        user_id: int,
        series_id: int,
    ) -> Sequence[TaskReminder]:
        """Return template reminders attached to a series."""
        return sorted(
            (
                reminder
                for reminder in self.reminders.values()
                if reminder.user_id == user_id and reminder.series_id == series_id
            ),
            key=lambda reminder: reminder.id,
        )

    async def add_reminder(self, reminder: TaskReminder) -> TaskReminder:
        """Persist a reminder."""
        return self.insert_reminder(reminder)

    async def add_reminders(self, reminders: Sequence[TaskReminder]) -> None:
        """Persist multiple reminders in one batch."""
        self.bulk_calls.append("add_reminders")
        for reminder in reminders:
            self.insert_reminder(reminder)

    async def delete_task_reminders(self, user_id: int, task_id: int) -> None:
        """Delete task reminders and their deliveries."""
        reminder_ids = {
            reminder.id
            for reminder in self.reminders.values()
            if reminder.user_id == user_id and reminder.task_id == task_id
        }
        for reminder_id in reminder_ids:
            self.reminders.pop(reminder_id, None)
        self.deliveries = {
            delivery_id: delivery
            for delivery_id, delivery in self.deliveries.items()
            if delivery.reminder_id not in reminder_ids
        }

    async def delete_series_reminders(self, user_id: int, series_id: int) -> None:
        """Delete series template reminders."""
        reminder_ids = {
            reminder.id
            for reminder in self.reminders.values()
            if reminder.user_id == user_id and reminder.series_id == series_id
        }
        for reminder_id in reminder_ids:
            self.reminders.pop(reminder_id, None)

    async def cancel_task_reminders(self, user_id: int, task_id: int) -> None:
        """Clear task reminder slots and cancel active deliveries."""
        reminder_ids = {
            reminder.id
            for reminder in self.reminders.values()
            if reminder.user_id == user_id and reminder.task_id == task_id
        }
        for reminder_id in reminder_ids:
            self.reminders[reminder_id].next_run_at = None
        self._cancel_deliveries(reminder_ids)

    async def cancel_series_reminders(self, user_id: int, series_id: int) -> None:
        """Clear series reminder slots and cancel active deliveries."""
        reminder_ids = {
            reminder.id
            for reminder in self.reminders.values()
            if reminder.user_id == user_id and reminder.series_id == series_id
        }
        for reminder_id in reminder_ids:
            self.reminders[reminder_id].next_run_at = None
        self._cancel_deliveries(reminder_ids)

    async def pause_project_reminders(self, user_id: int, project_id: int) -> None:
        """Clear reminder slots and cancel deliveries for a project."""
        task_ids = {
            task.id
            for task in self.tasks.values()
            if task.user_id == user_id and task.project_id == project_id
        }
        series_ids = {
            series.id
            for series in self.series.values()
            if series.user_id == user_id and series.project_id == project_id
        }
        reminder_ids = {
            reminder.id
            for reminder in self.reminders.values()
            if reminder.user_id == user_id
            and (reminder.task_id in task_ids or reminder.series_id in series_ids)
        }
        for reminder_id in reminder_ids:
            self.reminders[reminder_id].next_run_at = None
        self._cancel_deliveries(reminder_ids)

    async def resume_project_reminders(
        self,
        user_id: int,
        project_id: int,
        now: datetime,
    ) -> None:
        """Recompute future reminder slots for a reactivated project."""
        for series in self.series.values():
            if (
                series.user_id != user_id
                or series.project_id != project_id
                or series.deleted_at is not None
            ):
                continue
            for reminder in self.reminders.values():
                if reminder.user_id == user_id and reminder.series_id == series.id:
                    reminder.next_run_at = None
                    if reminder.kind == ReminderKind.SCHEDULED.value:
                        reminder.timezone = series.timezone
        for task in self.tasks.values():
            if (
                task.user_id != user_id
                or task.project_id != project_id
                or task.deleted_at is not None
                or task.completed_at is not None
            ):
                continue
            for reminder in self.reminders.values():
                if reminder.user_id != user_id or reminder.task_id != task.id:
                    continue
                reminder.next_run_at = rebuild_reminder_slot(
                    reminder,
                    due_at=task.due_at,
                    now=now,
                )

    async def project_is_active(self, user_id: int, project_id: int) -> bool:
        """Return whether a project exists and is neither archived nor deleted."""
        project = self.projects.get(project_id)
        return (
            project is not None
            and project.user_id == user_id
            and project.deleted_at is None
            and project.archived_at is None
        )

    async def list_series(self, user_id: int) -> Sequence[TaskSeries]:
        """List non-deleted series owned by a user."""
        return sorted(
            (
                series
                for series in self.series.values()
                if series.user_id == user_id and series.deleted_at is None
            ),
            key=lambda series: series.id,
        )

    async def get_series(
        self,
        user_id: int,
        series_id: int,
        *,
        for_update: bool = False,
    ) -> TaskSeries | None:
        """Return a non-deleted series owned by a user."""
        series = self.series.get(series_id)
        if series is None or series.user_id != user_id or series.deleted_at is not None:
            return None
        return series

    async def add_series(self, series: TaskSeries) -> TaskSeries:
        """Persist a series."""
        return self.insert_series(series)

    async def soft_delete_series(
        self,
        series: TaskSeries,
        deleted_at: datetime,
        batch_id: UUID | None = None,
    ) -> None:
        """Soft-delete a series."""
        series.deleted_at = deleted_at
        series.deletion_batch_id = batch_id

    async def add_series_labels(
        self,
        series_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """Assign labels to a series."""
        for label_id in label_ids:
            self.series_labels.add((series_id, label_id))

    async def replace_series_labels(
        self,
        series_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """Replace all labels assigned to a series."""
        self.series_labels = {pair for pair in self.series_labels if pair[0] != series_id}
        await self.add_series_labels(series_id, user_id, label_ids)

    async def list_series_labels(self, user_id: int, series_id: int) -> Sequence[Label]:
        """Return labels assigned to a series."""
        return [
            self.labels[label_id]
            for pair_series_id, label_id in sorted(self.series_labels)
            if pair_series_id == series_id
            and label_id in self.labels
            and self.labels[label_id].user_id == user_id
            and self.labels[label_id].deleted_at is None
        ]

    async def list_series_for_generation(self, now: datetime) -> Sequence[TaskSeries]:
        """List active series that may need new instances."""
        threshold = now + timedelta(days=HORIZON_DAYS) - timedelta(days=1)
        utc_today = now.astimezone(UTC).date()
        return sorted(
            (
                series
                for series in self.series.values()
                if series.deleted_at is None
                and (series.generated_through is None or series.generated_through < threshold)
                and (series.ends_on is None or series.ends_on >= utc_today - timedelta(days=1))
                and self._project_is_active(series.project_id)
            ),
            key=lambda series: series.id,
        )

    async def claim_series_for_generation(
        self,
        series_id: int,
        user_id: int,
    ) -> TaskSeries | None:
        """Return a series for generation unless the fake marks it locked."""
        self.claimed_series_ids.append(series_id)
        if series_id in self.series_errors:
            raise self.series_errors[series_id]
        if series_id in self.blocked_series_ids:
            return None
        series = self.series.get(series_id)
        if series is None or series.user_id != user_id or series.deleted_at is not None:
            return None
        return series

    async def get_series_tasks_for_update(
        self,
        series_id: int,
        user_id: int,
    ) -> Sequence[Task]:
        """Return non-deleted tasks of a series."""
        return sorted(
            (
                task
                for task in self.tasks.values()
                if task.series_id == series_id
                and task.user_id == user_id
                and task.deleted_at is None
            ),
            key=lambda task: (task.due_at, task.id),
        )

    async def list_future_series_tasks(
        self,
        series_id: int,
        user_id: int,
        now: datetime,
    ) -> Sequence[Task]:
        """Return future, incomplete, non-deleted instances of a series."""
        return sorted(
            (
                task
                for task in self.tasks.values()
                if task.series_id == series_id
                and task.user_id == user_id
                and task.deleted_at is None
                and task.completed_at is None
                and task.due_at > now
            ),
            key=lambda task: (task.due_at, task.id),
        )

    async def list_series_due_times(
        self,
        series_id: int,
        user_id: int,
        *,
        now: datetime,
    ) -> Sequence[datetime]:
        """Return due times already generated for a series at or after now."""
        return [
            task.due_at
            for task in self.tasks.values()
            if task.series_id == series_id
            and task.user_id == user_id
            and task.deleted_at is None
            and task.due_at >= now
        ]

    async def list_due_reminders(self, now: datetime) -> Sequence[TaskReminder]:
        """List due reminders whose owner is active and enabled."""
        due: list[TaskReminder] = []
        for reminder in self.reminders.values():
            if not reminder.enabled or reminder.next_run_at is None or reminder.next_run_at > now:
                continue
            if reminder.task_id is not None:
                task = self.tasks.get(reminder.task_id)
                if (
                    task is None
                    or task.user_id != reminder.user_id
                    or task.deleted_at is not None
                    or task.completed_at is not None
                    or not task.reminders_enabled
                    or not self._project_is_active(task.project_id)
                ):
                    continue
            elif reminder.series_id is not None:
                series = self.series.get(reminder.series_id)
                if (
                    series is None
                    or series.user_id != reminder.user_id
                    or series.deleted_at is not None
                    or not series.reminders_enabled
                    or not self._project_is_active(series.project_id)
                ):
                    continue
            else:
                continue
            due.append(reminder)
        return sorted(due, key=lambda reminder: (reminder.next_run_at, reminder.id))

    async def claim_due_reminder(
        self,
        reminder_id: int,
        now: datetime,
    ) -> TaskReminder | None:
        """Return a due reminder for simulated locking."""
        reminder = self.reminders.get(reminder_id)
        if (
            reminder is None
            or not reminder.enabled
            or reminder.next_run_at is None
            or reminder.next_run_at > now
        ):
            return None
        if self.on_claim is not None:
            self.on_claim()
        return reminder

    async def update_reminder_next_run_at(
        self,
        reminder: TaskReminder,
        next_run_at: datetime | None,
    ) -> None:
        """Update a reminder slot."""
        reminder.next_run_at = next_run_at

    async def get_delivery(
        self,
        reminder_id: int,
        scheduled_for: datetime,
    ) -> ReminderDelivery | None:
        """Return a delivery by its idempotency key."""
        return next(
            (
                delivery
                for delivery in self.deliveries.values()
                if delivery.reminder_id == reminder_id and delivery.scheduled_for == scheduled_for
            ),
            None,
        )

    async def add_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        """Persist a delivery."""
        return self.insert_delivery(delivery)

    async def mark_delivery_sending(
        self,
        delivery: ReminderDelivery,
        now: datetime,
    ) -> None:
        """Claim a delivery for sending."""
        delivery.status = DeliveryStatus.SENDING.value
        delivery.locked_at = now
        delivery.attempts += 1
        delivery.next_attempt_at = None

    async def mark_delivery_sent(
        self,
        delivery: ReminderDelivery,
        sent_at: datetime,
        telegram_message_id: int | None,
    ) -> None:
        """Mark a delivery as successfully sent."""
        delivery.status = DeliveryStatus.SENT.value
        delivery.sent_at = sent_at
        delivery.telegram_message_id = telegram_message_id
        delivery.locked_at = None
        delivery.next_attempt_at = None
        delivery.last_error = None

    async def mark_delivery_pending(
        self,
        delivery: ReminderDelivery,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> None:
        """Mark a delivery for a later retry."""
        delivery.status = DeliveryStatus.PENDING.value
        delivery.next_attempt_at = next_attempt_at
        delivery.locked_at = None
        delivery.last_error = last_error

    async def mark_delivery_failed(
        self,
        delivery: ReminderDelivery,
        next_attempt_at: datetime | None,
        last_error: str | None,
    ) -> None:
        """Mark a delivery as finally failed."""
        delivery.status = DeliveryStatus.FAILED.value
        delivery.next_attempt_at = next_attempt_at
        delivery.locked_at = None
        delivery.last_error = last_error

    async def cancel_delivery(self, delivery: ReminderDelivery) -> None:
        """Cancel a delivery without sending it."""
        delivery.status = DeliveryStatus.CANCELLED.value
        delivery.locked_at = None
        delivery.next_attempt_at = None

    async def reactivate_delivery(self, delivery: ReminderDelivery) -> None:
        """Reset a cancelled delivery to pending."""
        delivery.status = DeliveryStatus.PENDING.value
        delivery.attempts = 0
        delivery.locked_at = None
        delivery.next_attempt_at = None
        delivery.sent_at = None
        delivery.telegram_message_id = None
        delivery.last_error = None

    async def commit(self) -> None:
        """Record an explicit dispatcher or generation commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1

    async def flush(self) -> None:
        """Flush pending changes in the in-memory implementation."""


def fixed_now() -> datetime:
    """Return the fixed test clock value."""
    return datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def make_project(
    *,
    project_id: int | None = None,
    user_id: int = 7,
    name: str = "Work",
    archived_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> Project:
    """Build a project with deterministic values."""
    return Project(
        id=project_id,
        user_id=user_id,
        name=name,
        description=None,
        color_id=1,
        icon=None,
        archived_at=archived_at,
        deleted_at=deleted_at,
        deletion_batch_id=None,
    )


def make_label(
    *,
    label_id: int | None = None,
    user_id: int = 7,
    name: str = "Home",
    deleted_at: datetime | None = None,
) -> Label:
    """Build a label with deterministic values."""
    return Label(
        id=label_id,
        user_id=user_id,
        name=name,
        color_id=1,
        deleted_at=deleted_at,
    )


def make_task(
    *,
    task_id: int | None = None,
    user_id: int = 7,
    series_id: int | None = None,
    project_id: int | None = None,
    title: str = "Task",
    description: str | None = None,
    priority: str = "neither",
    due_at: datetime,
    completed_at: datetime | None = None,
    reminders_enabled: bool = False,
    deleted_at: datetime | None = None,
) -> Task:
    """Build a task with deterministic values."""
    return Task(
        id=task_id,
        user_id=user_id,
        series_id=series_id,
        project_id=project_id,
        title=title,
        description=description,
        priority=priority,
        due_at=due_at,
        completed_at=completed_at,
        reminders_enabled=reminders_enabled,
        deleted_at=deleted_at,
        deletion_batch_id=None,
    )


def make_series(
    *,
    series_id: int | None = None,
    user_id: int = 7,
    project_id: int | None = None,
    title: str = "Daily",
    description: str | None = None,
    priority: str = "neither",
    time_of_day: Any = None,
    timezone: str = "UTC",
    starts_on: Any = None,
    ends_on: Any = None,
    generated_through: datetime | None = None,
    reminders_enabled: bool = False,
    deleted_at: datetime | None = None,
) -> TaskSeries:
    """Build a series with deterministic values."""
    return TaskSeries(
        id=series_id,
        user_id=user_id,
        project_id=project_id,
        title=title,
        description=description,
        priority=priority,
        frequency="daily",
        time_of_day=time_of_day,
        timezone=timezone,
        starts_on=starts_on,
        ends_on=ends_on,
        generated_through=generated_through,
        reminders_enabled=reminders_enabled,
        deleted_at=deleted_at,
        deletion_batch_id=None,
    )


def make_reminder(
    *,
    reminder_id: int | None = None,
    user_id: int = 7,
    task_id: int | None = None,
    series_id: int | None = None,
    kind: ReminderKind = ReminderKind.BEFORE_DUE,
    enabled: bool = True,
    offset_minutes: int | None = 30,
    interval_days: int | None = None,
    time_of_day: Any = None,
    timezone: str | None = None,
    next_run_at: datetime | None = None,
) -> TaskReminder:
    """Build a reminder with deterministic values."""
    return TaskReminder(
        id=reminder_id,
        user_id=user_id,
        task_id=task_id,
        series_id=series_id,
        kind=kind.value,
        enabled=enabled,
        offset_minutes=offset_minutes,
        interval_days=interval_days,
        time_of_day=time_of_day,
        timezone=timezone,
        next_run_at=next_run_at,
    )


def make_delivery(
    *,
    delivery_id: int | None = None,
    reminder_id: int,
    user_id: int = 7,
    scheduled_for: datetime,
    status: str = "pending",
    attempts: int = 0,
    locked_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
) -> ReminderDelivery:
    """Build a reminder delivery with deterministic values."""
    return ReminderDelivery(
        id=delivery_id,
        reminder_id=reminder_id,
        user_id=user_id,
        scheduled_for=scheduled_for,
        status=status,
        attempts=attempts,
        locked_at=locked_at,
        next_attempt_at=next_attempt_at,
        sent_at=None,
        telegram_message_id=None,
        last_error=None,
    )
