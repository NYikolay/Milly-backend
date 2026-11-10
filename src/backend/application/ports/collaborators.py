"""Narrow collaborator ports used by task-management application services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, time
from typing import Protocol

from backend.domain.entities import ColorPalette, Label, Project, Task, TaskSeries
from backend.domain.enums import Priority, SeriesFrequency
from backend.domain.task_management.models import (
    UNSET,
    ReminderInput,
    SeriesDetail,
    UnsetType,
)


class ProjectReader(Protocol):
    """
    Read-only project contract consumed by task, series, and reminder services.

    Purpose:
        Replace a concrete project service dependency with the minimal project
        lookups needed for ownership and lifecycle checks.

    Notes:
        - Implementations return ``None`` for missing or cross-user projects.
        - Performs no writes and does not commit.
    """

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
            Resolve a project for validation or enrichment.

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

    async def get_projects_by_ids(
        self,
        user_id: int,
        project_ids: Sequence[int],
    ) -> dict[int, Project]:
        """
        Load non-deleted projects grouped by identifier.

        Purpose:
            Enrich task details with project data in one query.

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


class LabelReader(Protocol):
    """
    Read-only label contract consumed by task and series services.

    Purpose:
        Replace a concrete label service dependency with the minimal
        ownership-checked label lookups needed before assignment.

    Notes:
        - Implementations omit unknown, soft-deleted, and cross-user labels.
        - Performs no writes and does not commit.
    """

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
            Resolve a label for validation or enrichment.

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
            Validate a label selection before assigning it to a task or series.

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


class TaskReader(Protocol):
    """
    Read-only task contract consumed by the reminder dispatcher.

    Purpose:
        Replace a concrete task service dependency with the minimal task lookup
        needed to resolve reminder content and ownership.

    Notes:
        - Implementations return ``None`` for missing or cross-user tasks.
        - Performs no writes and does not commit.
    """

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
            Resolve a task for reminder delivery or validation.

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


class SeriesReader(Protocol):
    """
    Read-only series contract consumed by the reminder dispatcher.

    Purpose:
        Replace a concrete series service dependency with the minimal series
        lookup needed to resolve reminder content and ownership.

    Notes:
        - Implementations return ``None`` for missing or cross-user series.
        - Performs no writes and does not commit.
    """

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
            Resolve a series for reminder delivery or recurrence inspection.

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


class SeriesOperations(Protocol):
    """
    Series use-case contract consumed by the task service.

    Purpose:
        Let task workflows convert, update, detach, and delete series without
        depending on the concrete series service implementation.

    Notes:
        - Implementations own their validation and persistence but never
          commit; the UnitOfWork owns the transaction boundary.
    """

    async def get(self, user_id: int, series_id: int) -> TaskSeries:
        """
        Load one owned series or fail.

        Purpose:
            Resolve a series for a detail response or task workflow.

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
        ...

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
        ...

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
            BadRequestError: If the schedule, timezone, dates, or generation
                range is invalid, or the task project is archived.
            NotFoundError: If the task project or one of its labels does not
                exist for this user.

        Notes:
            - Flushes through the persistence ports but does not commit.
        """
        ...

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
            - Physically deletes future incomplete instances and soft-deletes
              the series and, when applicable, the selected task.
            - Flushes but does not commit.
        """
        ...

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
            history and regenerate future instances when the schedule changes.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.
            title: New title; ``None`` keeps the current title.
            description: New description; omit to keep and pass ``None`` to
                clear it.
            priority: New priority snapshot.
            frequency: New frequency; ``None`` keeps the current frequency.
            interval_days: New number of days between periodic occurrences.
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
            BadRequestError: If a field value or reminder rule is invalid.

        Notes:
            - A schedule change soft-deletes old future incomplete instances
              and regenerates replacements from the current time.
            - Other changes update future incomplete snapshots, labels, and
              reminder templates.
            - Flushes but does not commit.
        """
        ...

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
            - Cancels template reminder slots and deliveries and clears the
              current task's series reference.
            - Flushes but does not commit.
        """
        ...


class PalettePolicy(Protocol):
    """
    Palette validation contract consumed by project and label services.

    Purpose:
        Resolve colors and enforce the last-active-color invariant without
        depending on the concrete palette service implementation.

    Notes:
        - Implementations raise domain errors for missing or unavailable
          colors.
        - Performs no writes and does not commit.
    """

    async def get_color(self, color_id: int) -> ColorPalette:
        """
        Load a palette color for display.

        Purpose:
            Return the stored color of an existing project or label even when
            the palette entry is no longer active.

        Args:
            color_id: Palette color identifier.

        Returns:
            ColorPalette: The matching color regardless of its active state.

        Raises:
            NotFoundError: If the color does not exist.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def get_colors(self, color_ids: Sequence[int]) -> dict[int, ColorPalette]:
        """
        Load multiple palette colors for display.

        Purpose:
            Resolve the stored colors of a project page in one query while
            preserving the not-found contract of single lookups.

        Args:
            color_ids: Palette color identifiers to load.

        Returns:
            dict[int, ColorPalette]: Matching colors keyed by identifier,
                including colors that are no longer active.

        Raises:
            NotFoundError: If any requested color does not exist.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        ...

    async def resolve_for_write(self, color_id: int | None) -> ColorPalette:
        """
        Resolve the palette color for a new project or label.

        Purpose:
            Apply the default active color when no selection is provided and
            reject missing or inactive selections.

        Args:
            color_id: Requested palette color identifier, or ``None``.

        Returns:
            ColorPalette: The selected or default active color.

        Raises:
            BadRequestError: If the requested color is missing or inactive, or
                no default active color exists.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def ensure_active(self, color_id: int) -> ColorPalette:
        """
        Load an explicit color that must be active.

        Purpose:
            Validate a color change on an existing project or label.

        Args:
            color_id: Requested palette color identifier.

        Returns:
            ColorPalette: The matching active color.

        Raises:
            BadRequestError: If the color is missing or inactive.

        Notes:
            - Performs no writes and does not commit.
        """
        ...

    async def ensure_can_deactivate(self, color_id: int) -> None:
        """
        Ensure deactivating a color leaves another active color.

        Purpose:
            Protect the invariant that clients can always select a color.

        Args:
            color_id: Palette color about to be deactivated.

        Returns:
            None: The operation is allowed.

        Raises:
            ConflictError: If no other active color remains.

        Notes:
            - Deactivating an already inactive or missing color is allowed.
            - Performs no writes and does not commit.
        """
        ...

    async def ensure_can_delete(self, color_id: int) -> None:
        """
        Ensure deleting a color leaves another active color.

        Purpose:
            Protect the invariant that clients can always select a color.

        Args:
            color_id: Palette color about to be deleted.

        Returns:
            None: The operation is allowed.

        Raises:
            ConflictError: If no other active color remains.

        Notes:
            - Deleting an already inactive or missing color is allowed.
            - Performs no writes and does not commit.
        """
        ...


class ReminderSender(Protocol):
    """
    Delivery contract for sending a reminder message to a Telegram chat.

    Purpose:
        Keep the reminder dispatcher independent of the Telegram transport.

    Notes:
        - Implementations may raise transport-specific errors; the dispatcher
          classifies them for retry.
    """

    async def send(self, chat_id: int, text: str) -> int | None:
        """
        Send a reminder message.

        Purpose:
            Deliver rendered reminder text to the user's Telegram chat.

        Args:
            chat_id: Telegram chat identifier from the user's profile.
            text: Telegram HTML message text.

        Returns:
            int | None: Telegram message identifier when available.

        Raises:
            Exception: Implementation-specific delivery failures.

        Notes:
            - Has an external side effect; the dispatcher records the outcome
              in a delivery row.
        """
        ...
