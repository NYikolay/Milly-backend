from __future__ import annotations

from collections.abc import Sequence

from backend.application.ports.collaborators import ProjectReader
from backend.application.ports.task_management import SeriesRepository, TaskRepository
from backend.domain.entities import Task, TaskSeries
from backend.domain.enums import SeriesFrequency, TaskRecurrence
from backend.domain.task_management.models import TaskDetail

__all__ = ["TaskDetails"]


class TaskDetails:
    """Assembles task details with labels, reminders, project, and recurrence."""

    def __init__(
        self,
        task_repository: TaskRepository,
        series_repository: SeriesRepository,
        project_reader: ProjectReader,
    ) -> None:
        """
        Initialize the detail assembler with read-only collaborators.

        Purpose:
            Share task-detail assembly between task use cases without
            duplicating batch-query logic.

        Args:
            task_repository: Task-label and task-reminder reads.
            series_repository: Owning-series reads, including soft-deleted rows.
            project_reader: Project enrichment and batch project reads.
        """
        self._task_repository = task_repository
        self._series_repository = series_repository
        self._project_reader = project_reader

    async def detail(self, user_id: int, task: Task) -> TaskDetail:
        """
        Load the labels, reminders, project, and recurrence of a task.

        Purpose:
            Build the shared task-detail response shape.

        Args:
            user_id: Owner identifier.
            task: Task to enrich.

        Returns:
            TaskDetail: The task with its related data and recurrence mode.

        Notes:
            - Missing or archived projects still resolve as ``None`` only when
              the repository hides them; existing projects are returned.
            - The series is loaded including soft-deleted rows so history keeps
              its ``regular``/``periodic`` classification.
            - Performs no writes and does not commit.
        """
        labels = await self._task_repository.list_task_labels(user_id, task.id)
        reminders_by_task = await self._task_repository.list_task_reminders_for_tasks(
            user_id,
            (task.id,),
        )
        project = (
            await self._project_reader.get_project(user_id, task.project_id)
            if task.project_id is not None
            else None
        )
        series = (
            await self._series_repository.get_series(
                user_id,
                task.series_id,
                include_deleted=True,
            )
            if task.series_id is not None
            else None
        )
        return TaskDetail(
            task=task,
            labels=labels,
            reminders=reminders_by_task.get(task.id, ()),
            project=project,
            recurrence=self.classify_recurrence(series),
        )

    async def details(
        self,
        user_id: int,
        tasks: Sequence[Task],
    ) -> Sequence[TaskDetail]:
        """
        Load related data for a page of tasks with batch queries.

        Purpose:
            Avoid one label, reminder, project, or series query per task when
            building a task list response.

        Args:
            user_id: Owner identifier.
            tasks: Tasks belonging to the page.

        Returns:
            Sequence[TaskDetail]: The tasks with labels, reminders, projects,
                and recurrence modes attached in the same order as ``tasks``.

        Notes:
            - Returns immediately without issuing batch queries for an empty
              page.
            - The series batch query is skipped when no task has a series
              reference.
            - Series are loaded including soft-deleted rows so history keeps its
              ``regular``/``periodic`` classification.
            - Performs no writes and does not commit.
        """
        if not tasks:
            return []
        task_ids = tuple(task.id for task in tasks)
        project_ids = tuple(
            sorted({task.project_id for task in tasks if task.project_id is not None})
        )
        series_ids = tuple(sorted({task.series_id for task in tasks if task.series_id is not None}))
        labels_by_task = await self._task_repository.list_task_labels_for_tasks(
            user_id,
            task_ids,
        )
        reminders_by_task = await self._task_repository.list_task_reminders_for_tasks(
            user_id,
            task_ids,
        )
        projects_by_id = await self._project_reader.get_projects_by_ids(user_id, project_ids)
        series_by_id = (
            await self._series_repository.get_series_by_ids(
                user_id,
                series_ids,
                include_deleted=True,
            )
            if series_ids
            else {}
        )
        return [
            TaskDetail(
                task=task,
                labels=labels_by_task.get(task.id, ()),
                reminders=reminders_by_task.get(task.id, ()),
                project=(
                    projects_by_id.get(task.project_id) if task.project_id is not None else None
                ),
                recurrence=self.classify_recurrence(
                    series_by_id.get(task.series_id) if task.series_id is not None else None
                ),
            )
            for task in tasks
        ]

    def classify_recurrence(self, series: TaskSeries | None) -> TaskRecurrence:
        """
        Classify a task recurrence mode from its optional series.

        Purpose:
            Map the stored series frequency to the client-facing recurrence
            value while treating missing series as one-off.

        Args:
            series: Owning series including soft-deleted rows, or ``None``.

        Returns:
            TaskRecurrence: ``PERIODIC`` for periodic series, ``REGULAR`` for
                daily series, and ``ONE_OFF`` without a series or with an
                unexpected stored frequency.

        Notes:
            - The database check constraint limits frequencies to ``daily`` and
              ``periodic``; unexpected values are classified defensively as
              one-off instead of regular.
            - Performs no writes and does not commit.
        """
        if series is None:
            return TaskRecurrence.ONE_OFF
        if series.frequency == SeriesFrequency.PERIODIC.value:
            return TaskRecurrence.PERIODIC
        if series.frequency == SeriesFrequency.DAILY.value:
            return TaskRecurrence.REGULAR
        return TaskRecurrence.ONE_OFF
