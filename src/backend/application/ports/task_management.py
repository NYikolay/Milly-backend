"""Aggregate persistence ports for task-management application services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from backend.application.ports.unit_of_work import UnitOfWork as UnitOfWork
from backend.domain.entities import (
    ColorPalette,
    Label,
    Project,
    ReminderDelivery,
    Task,
    TaskLabel,
    TaskReminder,
    TaskSeries,
)


class PaletteRepository(Protocol):
    """
    Persistence contract for the global color palette.

    Purpose:
        Keep palette services independent of SQLAlchemy while exposing
        display-ordered reads and usage checks.

    Notes:
        - Implementations flush pending changes but never commit; the
          UnitOfWork owns the transaction boundary.
    """

    async def list_palette_colors(self) -> Sequence[ColorPalette]:
        """
        List every palette color in display order.

        Purpose:
            Expose the complete global palette for administrative consumers.

        Returns:
            Sequence[ColorPalette]: Colors ordered by ``(sort_order, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def list_active_palette_colors(self) -> Sequence[ColorPalette]:
        """
        List active palette colors in display order.

        Purpose:
            Provide the selectable client palette.

        Returns:
            Sequence[ColorPalette]: Active colors ordered by
                ``(sort_order, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_palette_color(self, color_id: int) -> ColorPalette | None:
        """
        Load one palette color by identifier.

        Purpose:
            Validate an explicit color selection and resolve display data.

        Args:
            color_id: Palette color identifier.

        Returns:
            ColorPalette | None: The matching color, or ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_palette_colors_by_ids(
        self,
        color_ids: Sequence[int],
    ) -> dict[int, ColorPalette]:
        """
        Load multiple palette colors by identifier.

        Purpose:
            Resolve project colors and label colors without one query per row.

        Args:
            color_ids: Palette color identifiers to load.

        Returns:
            dict[int, ColorPalette]: Matching colors keyed by identifier;
                unknown identifiers are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def get_default_palette_color(self) -> ColorPalette | None:
        """
        Load the first active palette color.

        Purpose:
            Resolve the color used when a project or label omits ``color_id``.

        Returns:
            ColorPalette | None: The first active color by
                ``(sort_order, id)``, or ``None`` when no color is active.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def count_active_palette_colors(self) -> int:
        """
        Count active palette colors.

        Purpose:
            Enforce the invariant that at least one palette color stays active.

        Returns:
            int: Number of colors whose ``is_active`` flag is set.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def palette_color_is_used(self, color_id: int) -> bool:
        """
        Check whether a palette color is referenced by a project or label.

        Purpose:
            Protect colors from deletion while foreign keys reference them.

        Args:
            color_id: Palette color identifier.

        Returns:
            bool: ``True`` when any project or label, including soft-deleted
                rows, references the color.

        Notes:
            - Performs no writes and does not commit.
        """
        ...


class AccountRepository(Protocol):
    """
    Persistence contract for account-level lookups used by task management.

    Purpose:
        Resolve user settings and Telegram identity without depending on the
        auth layer.

    Notes:
        - Read-only; implementations never commit.
    """

    async def get_user_timezone(self, user_id: int) -> str | None:
        """
        Load the IANA timezone of a user.

        Purpose:
            Convert a local task-list date into UTC day bounds and compute
            scheduled reminder slots without depending on the auth layer.

        Args:
            user_id: Owner identifier.

        Returns:
            str | None: The stored timezone name, or ``None`` when the user is
                missing.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_telegram_id(self, user_id: int) -> int | None:
        """
        Load the Telegram chat identifier of a user.

        Purpose:
            Address reminder deliveries to the external Telegram identity
            instead of the internal application user identifier.

        Args:
            user_id: Internal user identifier.

        Returns:
            int | None: The linked ``telegram_profiles.telegram_id``, or
                ``None`` when the user has no Telegram profile.

        Notes:
            - Performs no writes and does not commit.
        """
        ...


class ProjectRepository(Protocol):
    """
    Persistence contract for user-owned projects.

    Purpose:
        Expose tenant-scoped project reads, progress aggregates, and lifecycle
        mutations without leaking SQLAlchemy into application services.

    Notes:
        - Implementations flush pending changes but never commit.
        - Cross-user lookups return ``None`` or omit rows so services can raise
          ``NotFoundError`` without disclosing another user's data.
    """

    async def list_projects(
        self,
        user_id: int,
        *,
        include_archived: bool,
        limit: int,
        after_archived: bool | None,
        after_name: str | None,
        after_project_id: int | None,
    ) -> Sequence[tuple[Project, str]]:
        """
        List non-deleted projects owned by a user, at most one page.

        Purpose:
            Power the active project list and the project archive with
            deterministic cursor pagination.

        Args:
            user_id: Owner identifier.
            include_archived: Whether archived projects are included.
            limit: Maximum number of projects to return.
            after_archived: Cursor archive state of the last project on the
                previous page.
            after_name: Cursor case-insensitive name key of the last project on
                the previous page.
            after_project_id: Cursor identifier of the last project on the
                previous page.

        Returns:
            Sequence[tuple[Project, str]]: Non-deleted projects ordered by
                ``(is_archived, lower(name), id)`` paired with the
                ``lower(name)`` key produced by SQL.

        Notes:
            - The cursor predicate is applied only when every cursor value is
              provided.
            - The returned name key is the exact database value used for
              ordering so cursors remain consistent for non-ASCII names.
            - Performs no writes and does not commit.
        """
        ...

    async def get_project(
        self,
        user_id: int,
        project_id: int,
        *,
        for_update: bool = False,
    ) -> Project | None:
        """
        Load a non-deleted project owned by a user.

        Purpose:
            Resolve a project for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Project | None: The matching project, or ``None`` including when it
                belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_projects_by_ids(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, Project]:
        """
        Load non-deleted projects grouped by identifier.

        Purpose:
            Enrich a page of task details with project data in one query.

        Args:
            user_id: Owner identifier.
            project_ids: Project identifiers to load.

        Returns:
            dict[int, Project]: Owned non-deleted projects keyed by identifier;
                missing or cross-user projects are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def project_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_project_id: int | None = None,
    ) -> bool:
        """
        Check for an active project name owned by a user.

        Purpose:
            Enforce case-insensitive uniqueness among non-deleted,
            non-archived projects before writing.

        Args:
            user_id: Owner identifier.
            name: Candidate project name, already trimmed by the caller.
            exclude_project_id: Project to ignore during the comparison.

        Returns:
            bool: ``True`` when another active project has the same
                case-insensitive name.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def add_project(self, project: Project) -> Project:
        """
        Persist a new project.

        Purpose:
            Store a validated project in the current unit of work.

        Args:
            project: Project to insert.

        Returns:
            Project: The flushed project with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def save_project(self, project: Project) -> None:
        """
        Persist changes to an existing project.

        Purpose:
            Make service-level mutations of a loaded domain project durable in
            the current unit of work.

        Args:
            project: Project whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def count_projects(self, user_id: int, *, archived: bool) -> int:
        """
        Count non-deleted projects in one archive state.

        Purpose:
            Provide dashboard statistics for active and archived projects.

        Args:
            user_id: Owner identifier.
            archived: Whether to count archived instead of active projects.

        Returns:
            int: Number of matching non-deleted projects.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def project_progress(self, user_id: int, project_id: int) -> tuple[int, int]:
        """
        Count all and completed tasks of a project.

        Purpose:
            Build the project progress summary.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            tuple[int, int]: Total non-deleted task count and the subset with a
                completion timestamp.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def project_progress_for_projects(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, tuple[int, int]]:
        """
        Count all and completed tasks grouped by project.

        Purpose:
            Build a project page's progress summaries without one query per
            project.

        Args:
            user_id: Owner identifier.
            project_ids: Project identifiers to summarize.

        Returns:
            dict[int, tuple[int, int]]: Total non-deleted task count and the
                subset with a completion timestamp, keyed by project
                identifier; projects without tasks are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def today_progress(
        self,
        user_id: int,
        start: datetime,
        end: datetime,
    ) -> tuple[int, int]:
        """
        Count all and completed tasks due within a time window.

        Purpose:
            Build the "today" progress summary from UTC bounds of the user's
            local day.

        Args:
            user_id: Owner identifier.
            start: Inclusive lower bound for ``tasks.due_at``.
            end: Exclusive upper bound for ``tasks.due_at``.

        Returns:
            tuple[int, int]: Total non-deleted task count and the subset with a
                completion timestamp.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def planned_tasks_count(self, user_id: int) -> int:
        """
        Count incomplete tasks that belong to active plans.

        Purpose:
            Provide the dashboard count of planned tasks, including tasks
            without a project but excluding tasks in archived or deleted
            projects.

        Args:
            user_id: Owner identifier.

        Returns:
            int: Number of non-deleted, incomplete tasks in active projects or
                without a project.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def project_is_active(self, user_id: int, project_id: int) -> bool:
        """
        Check whether a project is active.

        Purpose:
            Gate reminder delivery and series generation on an unarchived,
            non-deleted project.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            bool: ``True`` when the project exists, is not deleted, and is not
                archived.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def soft_delete_project(
        self,
        project: Project,
        batch_id: UUID,
        deleted_at: datetime,
    ) -> None:
        """
        Soft-delete a project, its series, tasks, and pending deliveries.

        Purpose:
            Apply the project deletion batch atomically so deleted projects,
            generated series, future instances, and reminders stop being
            visible or deliverable.

        Args:
            project: Project to delete.
            batch_id: Identifier shared by every row deleted in this operation.
            deleted_at: Deletion timestamp written to every affected row.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Updates the project, its non-deleted series, and its non-deleted
              tasks with ``deleted_at`` and ``deletion_batch_id``.
            - Cancels ``pending``, ``failed``, and ``sending`` deliveries of
              reminders attached to the project's tasks and series, clearing
              their locks and retry slots.
            - Does not commit; the UnitOfWork owns the transaction.
        """
        ...


class LabelRepository(Protocol):
    """
    Persistence contract for user-owned labels.

    Purpose:
        Expose tenant-scoped label reads and lifecycle mutations without
        leaking SQLAlchemy into application services.

    Notes:
        - Implementations flush pending changes but never commit.
    """

    async def list_labels(self, user_id: int) -> Sequence[Label]:
        """
        List non-deleted labels owned by a user.

        Purpose:
            Power label selection and label management.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[Label]: Non-deleted labels in display order.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_label(
        self,
        user_id: int,
        label_id: int,
        *,
        for_update: bool = False,
    ) -> Label | None:
        """
        Load a non-deleted label owned by a user.

        Purpose:
            Resolve a label for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Label | None: The matching label, or ``None`` including when it
                belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_labels_by_ids(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> Sequence[Label]:
        """
        Load multiple non-deleted labels owned by a user.

        Purpose:
            Validate label selections before assigning them to tasks or series.

        Args:
            user_id: Owner identifier.
            label_ids: Requested label identifiers.

        Returns:
            Sequence[Label]: Matching owned labels; unknown or soft-deleted
                identifiers are omitted.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def label_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_label_id: int | None = None,
    ) -> bool:
        """
        Check for an active label name owned by a user.

        Purpose:
            Enforce case-insensitive uniqueness among non-deleted labels before
            writing.

        Args:
            user_id: Owner identifier.
            name: Candidate label name, already trimmed by the caller.
            exclude_label_id: Label to ignore during the comparison.

        Returns:
            bool: ``True`` when another non-deleted label has the same
                case-insensitive name.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def add_label(self, label: Label) -> Label:
        """
        Persist a new label.

        Purpose:
            Store a validated label in the current unit of work.

        Args:
            label: Label to insert.

        Returns:
            Label: The flushed label with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def save_label(self, label: Label) -> None:
        """
        Persist changes to an existing label.

        Purpose:
            Make service-level mutations of a loaded domain label durable in
            the current unit of work.

        Args:
            label: Label whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def soft_delete_label(self, label: Label, deleted_at: datetime) -> None:
        """
        Soft-delete a label.

        Purpose:
            Hide a label from selection and task responses while preserving
            its task and series assignments.

        Args:
            label: Label to delete.
            deleted_at: Deletion timestamp.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit; the UnitOfWork owns the transaction.
        """
        ...


class TaskRepository(Protocol):
    """
    Persistence contract for user-owned tasks and task-label rows.

    Purpose:
        Expose tenant-scoped task reads, batch detail loaders, and lifecycle
        mutations without leaking SQLAlchemy into application services.

    Notes:
        - Implementations flush pending changes but never commit.
    """

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
        """
        List non-deleted tasks using the service-level filters.

        Purpose:
            Power the task list with date, project, priority, status,
            recurrence, text, and archive filters, returning at most one page.

        Args:
            user_id: Owner identifier.
            date_start: Inclusive lower bound for ``due_at``.
            date_end: Exclusive upper bound for ``due_at``.
            project_ids: Restrict to these project identifiers when non-empty.
            priorities: Restrict to these priority values when non-empty.
            status: One of ``all``, ``open``, ``completed``, or ``overdue``.
            recurrence: One of ``all``, ``regular``, ``periodic``, or
                ``one_off``; ``regular`` matches daily series and ``periodic``
                matches interval series.
            query: Case-insensitive title/description substring.
            include_archived_projects: Whether tasks in archived projects are
                included.
            now: Current instant used to evaluate the overdue status.
            limit: Maximum number of tasks to return.
            after_due_at: Cursor due time of the last task on the previous page.
            after_task_id: Cursor identifier of the last task on the previous
                page.

        Returns:
            Sequence[Task]: Matching tasks ordered by ``(due_at, id)``.

        Notes:
            - The cursor predicate is applied only when both cursor values are
              provided.
            - Soft-deleted series still determine the recurrence of their
              generated tasks.
            - Performs no writes and does not commit.
        """
        ...

    async def get_task(
        self,
        user_id: int,
        task_id: int,
        *,
        for_update: bool = False,
    ) -> Task | None:
        """
        Load a non-deleted task owned by a user.

        Purpose:
            Resolve a task for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Task | None: The matching task, or ``None`` including when it
                belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def add_task(self, task: Task) -> Task:
        """
        Persist a new task.

        Purpose:
            Store a validated one-off task or generated series instance.

        Args:
            task: Task to insert.

        Returns:
            Task: The flushed task with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def add_tasks(self, tasks: Sequence[Task]) -> Sequence[Task]:
        """
        Persist multiple new tasks in one batch.

        Purpose:
            Materialize generated series instances without one flush per task.

        Args:
            tasks: Tasks to insert.

        Returns:
            Sequence[Task]: The same tasks after their identifiers are
                generated.

        Notes:
            - Empty input is a no-op and does not flush.
            - Flushes once but does not commit.
        """
        ...

    async def save_task(self, task: Task) -> None:
        """
        Persist changes to an existing task.

        Purpose:
            Make service-level mutations of a loaded domain task durable in the
            current unit of work.

        Args:
            task: Task whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def soft_delete_task(
        self,
        task: Task,
        deleted_at: datetime,
        batch_id: UUID | None = None,
    ) -> None:
        """
        Soft-delete a task.

        Purpose:
            Hide a task from reads while preserving its history.

        Args:
            task: Task to delete.
            deleted_at: Deletion timestamp.
            batch_id: Optional deletion batch shared with related rows.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def hard_delete_tasks(self, task_ids: Sequence[int], user_id: int) -> None:
        """
        Physically delete owned tasks and their dependent rows.

        Purpose:
            Remove generated instances that no longer belong to a series edit
            or a deleted series while relying on foreign-key cascades for
            labels, reminders, and deliveries.

        Args:
            task_ids: Task identifiers to delete.
            user_id: Owner identifier; tasks of other users are unaffected.

        Returns:
            None: The rows are removed from the current unit of work.

        Notes:
            - Empty input is a no-op and does not flush.
            - Deletes tasks physically, unlike ``soft_delete_task``.
            - Dependent task labels, reminders, and deliveries are removed by
              database cascades.
            - Flushes but does not commit.
        """
        ...

    async def list_task_labels_for_tasks(
        self,
        user_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Sequence[Label]]:
        """
        List non-deleted labels grouped by task.

        Purpose:
            Enrich a page of task details without one label query per task.

        Args:
            user_id: Owner identifier.
            task_ids: Task identifiers to enrich.

        Returns:
            dict[int, Sequence[Label]]: Assigned labels keyed by task
                identifier; tasks without labels are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def list_task_reminders_for_tasks(
        self,
        user_id: int,
        task_ids: Sequence[int],
    ) -> dict[int, Sequence[TaskReminder]]:
        """
        List reminders grouped by task.

        Purpose:
            Enrich a page of task details without one reminder query per task.

        Args:
            user_id: Owner identifier.
            task_ids: Task identifiers to enrich.

        Returns:
            dict[int, Sequence[TaskReminder]]: Reminders keyed by task
                identifier; tasks without reminders are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def add_task_labels(
        self,
        task_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Assign labels to a task.

        Purpose:
            Copy a validated label selection into ``task_labels``.

        Args:
            task_id: Task identifier.
            user_id: Owner identifier.
            label_ids: Label identifiers to assign.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def add_task_label_rows(self, rows: Sequence[TaskLabel]) -> None:
        """
        Persist multiple task-label assignments in one batch.

        Purpose:
            Copy the series label set onto every generated instance.

        Args:
            rows: Task-label assignment rows to insert.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Empty input is a no-op and does not flush.
            - Flushes once but does not commit.
        """
        ...

    async def replace_task_labels(
        self,
        task_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Replace all labels assigned to a task.

        Purpose:
            Apply an updated label selection to an existing task.

        Args:
            task_id: Task identifier.
            user_id: Owner identifier.
            label_ids: Complete new label selection.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def list_task_labels(self, user_id: int, task_id: int) -> Sequence[Label]:
        """
        List non-deleted labels assigned to a task.

        Purpose:
            Build the label part of a task detail.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            Sequence[Label]: Assigned non-deleted labels ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def list_future_series_tasks(
        self,
        series_id: int,
        user_id: int,
        now: datetime,
    ) -> Sequence[Task]:
        """
        List future, incomplete, non-deleted instances of a series.

        Purpose:
            Select exactly the instances affected by series edits and deletes.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            now: Current instant used as the lower bound.

        Returns:
            Sequence[Task]: Matching tasks ordered by due time.

        Notes:
            - Performs no writes and does not commit.
        """
        ...


class SeriesRepository(Protocol):
    """
    Persistence contract for recurring task series.

    Purpose:
        Expose tenant-scoped series reads, generation claims, and lifecycle
        mutations without leaking SQLAlchemy into application services.

    Notes:
        - Implementations flush pending changes but never commit.
        - Project activity checks belong to ``ProjectReader``; task-label and
          reminder row inserts belong to the task and reminder repositories.
    """

    async def list_series(self, user_id: int) -> Sequence[TaskSeries]:
        """
        List non-deleted series owned by a user.

        Purpose:
            Power the recurring-task list.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[TaskSeries]: Non-deleted series ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_series(
        self,
        user_id: int,
        series_id: int,
        *,
        for_update: bool = False,
        include_deleted: bool = False,
    ) -> TaskSeries | None:
        """
        Load a series owned by a user.

        Purpose:
            Resolve a series for reads and serialize mutations when requested,
            optionally including soft-deleted rows for recurrence inspection.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.
            for_update: Whether to lock the row until the transaction ends.
            include_deleted: Whether soft-deleted series are returned.

        Returns:
            TaskSeries | None: The matching series, or ``None`` including when
                it belongs to another user or is soft-deleted while deletion is
                not requested.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_series_by_ids(
        self,
        user_id: int,
        series_ids: Sequence[int],
        *,
        include_deleted: bool = False,
    ) -> dict[int, TaskSeries]:
        """
        Load multiple series owned by a user, keyed by identifier.

        Purpose:
            Resolve series data for a page without one query per task,
            optionally including soft-deleted rows.

        Args:
            user_id: Owner identifier.
            series_ids: Series identifiers to load.
            include_deleted: Whether soft-deleted series are returned.

        Returns:
            dict[int, TaskSeries]: Owned series keyed by identifier; missing or
                cross-user series are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def add_series(self, series: TaskSeries) -> TaskSeries:
        """
        Persist a new series.

        Purpose:
            Store a validated recurring-task template.

        Args:
            series: Series to insert.

        Returns:
            TaskSeries: The flushed series with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def save_series(self, series: TaskSeries) -> None:
        """
        Persist changes to an existing series.

        Purpose:
            Make service-level mutations of a loaded domain series durable in
            the current unit of work.

        Args:
            series: Series whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def soft_delete_series(
        self,
        series: TaskSeries,
        deleted_at: datetime,
        batch_id: UUID | None = None,
    ) -> None:
        """
        Soft-delete a series.

        Purpose:
            Stop generation and hide the series from reads while preserving
            past instances.

        Args:
            series: Series to delete.
            deleted_at: Deletion timestamp.
            batch_id: Optional deletion batch shared with related rows.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def add_series_labels(
        self,
        series_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Assign labels to a series template.

        Purpose:
            Store the label set copied into generated instances.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            label_ids: Label identifiers to assign.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def replace_series_labels(
        self,
        series_id: int,
        user_id: int,
        label_ids: Sequence[int],
    ) -> None:
        """
        Replace all labels assigned to a series template.

        Purpose:
            Apply an updated label selection to a series.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            label_ids: Complete new label selection.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def list_series_labels(self, user_id: int, series_id: int) -> Sequence[Label]:
        """
        List non-deleted labels assigned to a series.

        Purpose:
            Build the label part of a series detail.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            Sequence[Label]: Assigned non-deleted labels ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def list_series_for_generation(self, now: datetime) -> Sequence[TaskSeries]:
        """
        List series that may need new instances.

        Purpose:
            Select non-deleted series that are not caught up, have not ended,
            and belong to an active project.

        Args:
            now: Current instant used to evaluate the generation horizon.

        Returns:
            Sequence[TaskSeries]: Eligible series ordered by identifier.

        Notes:
            - The generation marker must be missing or older than one day
              before the 90-day horizon.
            - Series ending before the previous UTC day are ignored.
            - Future series without a marker are eligible so their first
              occurrence can be materialized.
            - Performs no writes and does not commit.
        """
        ...

    async def claim_series_for_generation(
        self,
        series_id: int,
        user_id: int,
    ) -> TaskSeries | None:
        """
        Claim a series for generation with a non-blocking row lock.

        Purpose:
            Serialize overlapping generation workers and concurrent series
            edits with ``FOR UPDATE SKIP LOCKED``.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.

        Returns:
            TaskSeries | None: The locked non-deleted series, or ``None`` when
                another transaction holds the row.

        Notes:
            - Locks the ``task_series`` row until the transaction ends.
            - A locked series is skipped until the next generation run.
            - Flushes but does not commit.
        """
        ...

    async def get_series_tasks_for_update(
        self,
        series_id: int,
        user_id: int,
    ) -> Sequence[Task]:
        """
        Load non-deleted tasks of a series with a row lock.

        Purpose:
            Serialize series-wide updates and deletions.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.

        Returns:
            Sequence[Task]: Non-deleted series tasks ordered by due time.

        Notes:
            - Locks the selected task rows until the transaction ends.
            - Flushes but does not commit.
        """
        ...

    async def series_due_slot_exists(
        self,
        series_id: int,
        user_id: int,
        due_at: datetime,
        *,
        exclude_task_id: int | None = None,
    ) -> bool:
        """
        Check whether a series already owns a non-deleted task at a due instant.

        Purpose:
            Preserve the historical series link on completed-instance edits
            while avoiding a unique ``(series_id, due_at)`` slot collision.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            due_at: Candidate timezone-aware due instant.
            exclude_task_id: Task to ignore during the comparison, usually the
                task being edited.

        Returns:
            bool: ``True`` when another non-deleted task of the series occupies
                the slot.

        Notes:
            - Includes completed tasks because the partial unique index covers
              every non-deleted row.
            - Performs no writes and does not commit.
        """
        ...

    async def list_series_due_times(
        self,
        series_id: int,
        user_id: int,
        *,
        now: datetime,
    ) -> Sequence[datetime]:
        """
        List due times already generated for a series.

        Purpose:
            Make instance generation idempotent for existing slots without
            loading the past.

        Args:
            series_id: Series identifier.
            user_id: Owner identifier.
            now: Current instant used as the lower bound.

        Returns:
            Sequence[datetime]: Due instants of non-deleted series tasks at or
                after ``now``.

        Notes:
            - Performs no writes and does not commit.
        """
        ...


class ReminderRepository(Protocol):
    """
    Persistence contract for task and series reminders and their deliveries.

    Purpose:
        Expose tenant-scoped reminder reads, generation of delivery rows, and
        delivery state transitions without leaking SQLAlchemy into application
        services.

    Notes:
        - Implementations flush pending changes but never commit.
        - Delivery claim and outcome transitions keep their exact ordering and
          flush-only behavior; the dispatcher commits through ``UnitOfWork``.
    """

    async def list_task_reminders(
        self,
        user_id: int,
        task_id: int,
    ) -> Sequence[TaskReminder]:
        """
        List reminders attached to a task.

        Purpose:
            Build the reminder part of a task detail and recompute slots.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            Sequence[TaskReminder]: Task reminders ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def list_series_reminders(
        self,
        user_id: int,
        series_id: int,
    ) -> Sequence[TaskReminder]:
        """
        List template reminders attached to a series.

        Purpose:
            Copy the reminder configuration into generated instances.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            Sequence[TaskReminder]: Template reminders ordered by identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def add_reminder(self, reminder: TaskReminder) -> TaskReminder:
        """
        Persist a task or series reminder.

        Purpose:
            Store a reminder rule in the current unit of work.

        Args:
            reminder: Reminder to insert.

        Returns:
            TaskReminder: The flushed reminder with its generated identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def add_reminders(self, reminders: Sequence[TaskReminder]) -> None:
        """
        Persist multiple reminders in one batch.

        Purpose:
            Copy template reminders onto generated series instances without one
            flush per reminder.

        Args:
            reminders: Reminders to insert.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Empty input is a no-op and does not flush.
            - Flushes once but does not commit.
        """
        ...

    async def save_reminder(self, reminder: TaskReminder) -> None:
        """
        Persist changes to an existing reminder.

        Purpose:
            Make service-level mutations of a loaded domain reminder durable in
            the current unit of work.

        Args:
            reminder: Reminder whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def delete_task_reminders(self, user_id: int, task_id: int) -> None:
        """
        Delete every reminder attached to a task.

        Purpose:
            Rebuild task reminders after a due-time or configuration change.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def delete_series_reminders(self, user_id: int, series_id: int) -> None:
        """
        Delete every template reminder attached to a series.

        Purpose:
            Replace the reminder template after a series update.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def cancel_task_reminders(self, user_id: int, task_id: int) -> None:
        """
        Clear task reminder slots and cancel their active deliveries.

        Purpose:
            Stop delivery after completion, deletion, or schedule changes.

        Args:
            user_id: Owner identifier.
            task_id: Task identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Sets ``next_run_at`` to ``None`` and cancels ``pending``,
              ``failed``, and ``sending`` deliveries.
            - Flushes but does not commit.
        """
        ...

    async def cancel_series_reminders(self, user_id: int, series_id: int) -> None:
        """
        Clear series reminder slots and cancel their active deliveries.

        Purpose:
            Stop delivery for template and instance reminders of a series.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Sets ``next_run_at`` to ``None`` and cancels ``pending``,
              ``failed``, and ``sending`` deliveries.
            - Flushes but does not commit.
        """
        ...

    async def pause_project_reminders(self, user_id: int, project_id: int) -> None:
        """
        Pause reminders attached to a project.

        Purpose:
            Stop delivery while a project is archived.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            None: The changes stay in the current unit of work.

        Notes:
            - Sets ``next_run_at`` to ``None`` for task and series reminders and
              cancels active deliveries.
            - Flushes but does not commit.
        """
        ...

    async def resume_project_reminders(
        self,
        user_id: int,
        project_id: int,
        now: datetime,
    ) -> None:
        """
        Recompute reminder slots for a project after unarchiving.

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
            - Template series reminders are retargeted to the series timezone
              and keep ``next_run_at=None``.
            - Incomplete task reminders, including overdue tasks, are
              recomputed: before-due slots come from ``due_at`` and become
              ``None`` when past; scheduled slots advance to the first suitable
              slot after ``now``.
            - Cancelled deliveries are left for the dispatcher to reactivate
              when their recomputed slot becomes current.
            - Flushes but does not commit.
        """
        ...

    async def list_due_reminders(
        self,
        now: datetime,
        *,
        limit: int,
    ) -> Sequence[TaskReminder]:
        """
        List due, enabled reminders with active owners.

        Purpose:
            Feed the reminder dispatcher only rules that may be delivered.

        Args:
            now: Current instant used to select due slots.
            limit: Maximum number of reminders to return.

        Returns:
            Sequence[TaskReminder]: Due reminders ordered by slot and
                identifier.

        Notes:
            - Requires the reminder to be enabled, its master toggle to be on,
              its task or series to be active, and its project not to be
              archived or deleted.
            - Performs no writes and does not commit.
        """
        ...

    async def claim_due_reminder(
        self,
        reminder_id: int,
        now: datetime,
    ) -> TaskReminder | None:
        """
        Claim one due reminder for delivery.

        Purpose:
            Serialize workers with ``FOR UPDATE SKIP LOCKED`` so two workers do
            not deliver the same reminder slot.

        Args:
            reminder_id: Reminder identifier.
            now: Current instant used to verify the slot is still due.

        Returns:
            TaskReminder | None: The locked reminder, or ``None`` when it is no
                longer due or was claimed by another worker.

        Notes:
            - Locks the ``task_reminders`` row until the transaction ends.
            - Flushes but does not commit; the dispatcher commits the claim.
        """
        ...

    async def update_reminder_next_run_at(
        self,
        reminder: TaskReminder,
        next_run_at: datetime | None,
    ) -> None:
        """
        Update a reminder's next slot.

        Purpose:
            Advance or clear a slot after a successful or final delivery.

        Args:
            reminder: Reminder to update.
            next_run_at: New slot, or ``None`` to stop delivery.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def get_delivery(
        self,
        reminder_id: int,
        scheduled_for: datetime,
    ) -> ReminderDelivery | None:
        """
        Load a delivery by its idempotency key.

        Purpose:
            Detect sent, cancelled, and retryable deliveries for a slot.

        Args:
            reminder_id: Reminder identifier.
            scheduled_for: Scheduled slot.

        Returns:
            ReminderDelivery | None: The matching delivery, or ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def add_delivery(self, delivery: ReminderDelivery) -> ReminderDelivery:
        """
        Persist a new delivery.

        Purpose:
            Create the idempotent delivery row for a reminder slot.

        Args:
            delivery: Delivery to insert.

        Returns:
            ReminderDelivery: The flushed delivery with its generated
                identifier.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def save_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Persist changes to an existing delivery.

        Purpose:
            Make service-level mutations of a loaded domain delivery durable in
            the current unit of work.

        Args:
            delivery: Delivery whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def mark_delivery_sending(
        self,
        delivery: ReminderDelivery,
        now: datetime,
    ) -> None:
        """
        Claim a delivery for sending.

        Purpose:
            Record the worker lease and increment the attempt counter.

        Args:
            delivery: Delivery to claim.
            now: Current instant written to ``locked_at``.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Sets ``status='sending'`` and clears ``next_attempt_at``.
            - Flushes but does not commit.
        """
        ...

    async def mark_delivery_sent(
        self,
        delivery: ReminderDelivery,
        sent_at: datetime,
        telegram_message_id: int | None,
    ) -> None:
        """
        Mark a delivery as sent.

        Purpose:
            Record successful Telegram delivery and clear the worker lease.

        Args:
            delivery: Delivery to update.
            sent_at: Success timestamp.
            telegram_message_id: Identifier returned by Telegram, if any.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Flushes but does not commit.
        """
        ...

    async def mark_delivery_pending(
        self,
        delivery: ReminderDelivery,
        next_attempt_at: datetime,
        last_error: str | None,
    ) -> None:
        """
        Mark a delivery for retry.

        Purpose:
            Apply a backoff after a retryable sender failure.

        Args:
            delivery: Delivery to update.
            next_attempt_at: Earliest retry instant.
            last_error: Safe short failure description.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Clears ``locked_at`` and flushes without committing.
        """
        ...

    async def mark_delivery_failed(
        self,
        delivery: ReminderDelivery,
        next_attempt_at: datetime | None,
        last_error: str | None,
    ) -> None:
        """
        Mark a delivery as finally failed.

        Purpose:
            Stop retries after the attempt budget is exhausted.

        Args:
            delivery: Delivery to update.
            next_attempt_at: Optional final retry instant; usually ``None``.
            last_error: Safe short failure description.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Clears ``locked_at`` and flushes without committing.
        """
        ...

    async def cancel_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Cancel one delivery without sending it.

        Purpose:
            Stop a claimed slot when the owning task, series, toggle, or project
            is no longer dispatchable.

        Args:
            delivery: Delivery to cancel.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Sets ``status='cancelled'`` and clears the lease and retry slot.
            - Flushes but does not commit.
        """
        ...

    async def reactivate_delivery(self, delivery: ReminderDelivery) -> None:
        """
        Reset a cancelled delivery to pending.

        Purpose:
            Reuse the unique ``(reminder_id, scheduled_for)`` row when a slot
            becomes deliverable again.

        Args:
            delivery: Cancelled delivery to reactivate.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Resets attempts, locks, retry slots, and send metadata.
            - Flushes but does not commit.
        """
        ...
