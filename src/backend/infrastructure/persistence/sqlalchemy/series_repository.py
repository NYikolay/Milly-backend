"""SQLAlchemy implementation of the series persistence port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import Label, Task, TaskSeries
from backend.domain.task_management.policies import HORIZON_DAYS
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_series,
    label_to_entity,
    series_to_entity,
    task_to_entity,
)
from backend.models.label import Label as LabelRow
from backend.models.project import Project as ProjectRow
from backend.models.series_label import SeriesLabel as SeriesLabelRow
from backend.models.task import Task as TaskRow
from backend.models.task_series import TaskSeries as TaskSeriesRow


class SqlAlchemySeriesRepository:
    """SQLAlchemy implementation of the series persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the series adapter with a request-scoped session.

        Purpose:
            Bind series reads and writes to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

    async def list_series(self, user_id: int) -> Sequence[TaskSeries]:
        """
        List non-deleted series owned by a user.

        Purpose:
            Power the recurring-task list.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[TaskSeries]: Non-deleted domain series ordered by
                identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(TaskSeriesRow)
            .where(
                TaskSeriesRow.user_id == user_id,
                TaskSeriesRow.deleted_at.is_(None),
            )
            .order_by(TaskSeriesRow.id)
        )
        return [series_to_entity(row) for row in result.scalars().all()]

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
            TaskSeries | None: The matching domain series, or ``None``
                including when it belongs to another user or is soft-deleted
                while deletion is not requested.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(TaskSeriesRow).where(
            TaskSeriesRow.id == series_id,
            TaskSeriesRow.user_id == user_id,
        )
        if not include_deleted:
            statement = statement.where(TaskSeriesRow.deleted_at.is_(None))
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return series_to_entity(row) if row is not None else None

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
            dict[int, TaskSeries]: Owned domain series keyed by identifier;
                missing or cross-user series are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not series_ids:
            return {}
        statement = select(TaskSeriesRow).where(
            TaskSeriesRow.user_id == user_id,
            TaskSeriesRow.id.in_(series_ids),
        )
        if not include_deleted:
            statement = statement.where(TaskSeriesRow.deleted_at.is_(None))
        result = await self._session.execute(statement)
        return {row.id: series_to_entity(row) for row in result.scalars().all()}

    async def add_series(self, series: TaskSeries) -> TaskSeries:
        """
        Persist a new series.

        Purpose:
            Store a validated recurring-task template.

        Args:
            series: Domain series to insert.

        Returns:
            TaskSeries: The flushed series mapped back from its ORM row with
                the generated identifier.

        Notes:
            - Inserts a fresh ORM row and never copies the transient domain
              identifier, so the database identity generates the key.
            - Writes the generated identifier back to the input entity.
            - Flushes but does not commit.
        """
        row = _new_series_row(series)
        self._session.add(row)
        await self._session.flush()
        if row.id is not None:
            series.id = row.id
        return series_to_entity(row)

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

        Raises:
            LookupError: If the series row is not present in the session.

        Notes:
            - A transient series with ``id == 0`` is inserted through
              ``add_series``.
            - Resolves the tracked row by primary key; the caller is
              responsible for loading the entity through a tenant-scoped read.
            - Flushes but does not commit.
        """
        if series.id == 0:
            await self.add_series(series)
            return
        row = await self._session.get(TaskSeriesRow, series.id)
        if row is None:
            raise LookupError(f"Series {series.id} is not present in the session")
        apply_series(series, row)
        await self._session.flush()

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
            series: Domain series to delete.
            deleted_at: Deletion timestamp.
            batch_id: Optional deletion batch shared with related rows.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Updates the series row by primary key, matching the previous
              ORM-flush behavior for an already tenant-scoped entity.
            - Flushes but does not commit.
        """
        series.deleted_at = deleted_at
        series.deletion_batch_id = batch_id
        await self._session.execute(
            update(TaskSeriesRow)
            .where(TaskSeriesRow.id == series.id)
            .values(deleted_at=deleted_at, deletion_batch_id=batch_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()

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
            - Flushes but does not commit, including for an empty selection.
        """
        self._session.add_all(
            SeriesLabelRow(series_id=series_id, label_id=label_id, user_id=user_id)
            for label_id in label_ids
        )
        await self._session.flush()

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
            - Deletes owned assignments first, then inserts the new selection
              through ``add_series_labels``.
            - Flushes but does not commit.
        """
        await self._session.execute(
            delete(SeriesLabelRow).where(
                SeriesLabelRow.series_id == series_id,
                SeriesLabelRow.user_id == user_id,
            )
        )
        await self.add_series_labels(series_id, user_id, label_ids)

    async def list_series_labels(self, user_id: int, series_id: int) -> Sequence[Label]:
        """
        List non-deleted labels assigned to a series.

        Purpose:
            Build the label part of a series detail.

        Args:
            user_id: Owner identifier.
            series_id: Series identifier.

        Returns:
            Sequence[Label]: Assigned non-deleted domain labels ordered by
                identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(LabelRow)
            .join(SeriesLabelRow, SeriesLabelRow.label_id == LabelRow.id)
            .where(
                SeriesLabelRow.series_id == series_id,
                SeriesLabelRow.user_id == user_id,
                LabelRow.user_id == user_id,
                LabelRow.deleted_at.is_(None),
            )
            .order_by(LabelRow.id)
        )
        return [label_to_entity(row) for row in result.scalars().all()]

    async def list_series_for_generation(self, now: datetime) -> Sequence[TaskSeries]:
        """
        List series that may need new instances.

        Purpose:
            Select non-deleted series that are not caught up, have not ended,
            and belong to an active project.

        Args:
            now: Current instant used to evaluate the generation horizon.

        Returns:
            Sequence[TaskSeries]: Eligible domain series ordered by identifier.

        Notes:
            - The generation marker must be missing or older than one day
              before the 90-day horizon.
            - Series ending before the previous UTC day are ignored.
            - Future series without a marker are eligible so their first
              occurrence can be materialized.
            - Performs no writes and does not commit.
        """
        threshold = now + timedelta(days=HORIZON_DAYS) - timedelta(days=1)
        utc_today = now.astimezone(UTC).date()
        active_project_ids = select(ProjectRow.id).where(
            ProjectRow.deleted_at.is_(None),
            ProjectRow.archived_at.is_(None),
        )
        result = await self._session.execute(
            select(TaskSeriesRow)
            .where(
                TaskSeriesRow.deleted_at.is_(None),
                or_(
                    TaskSeriesRow.generated_through.is_(None),
                    TaskSeriesRow.generated_through < threshold,
                ),
                or_(
                    TaskSeriesRow.ends_on.is_(None),
                    TaskSeriesRow.ends_on >= utc_today - timedelta(days=1),
                ),
                or_(
                    TaskSeriesRow.project_id.is_(None),
                    TaskSeriesRow.project_id.in_(active_project_ids),
                ),
            )
            .order_by(TaskSeriesRow.id)
        )
        return [series_to_entity(row) for row in result.scalars().all()]

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
            TaskSeries | None: The locked non-deleted domain series, or
                ``None`` when another transaction holds the row.

        Notes:
            - Locks the ``task_series`` row until the transaction ends.
            - A locked series is skipped until the next generation run.
            - Does not flush or commit.
        """
        result = await self._session.execute(
            select(TaskSeriesRow)
            .where(
                TaskSeriesRow.id == series_id,
                TaskSeriesRow.user_id == user_id,
                TaskSeriesRow.deleted_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        row = result.scalar_one_or_none()
        return series_to_entity(row) if row is not None else None

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
            Sequence[Task]: Non-deleted domain series tasks ordered by due
                time.

        Notes:
            - Locks the selected task rows until the transaction ends.
            - Does not flush or commit.
        """
        result = await self._session.execute(
            select(TaskRow)
            .where(
                TaskRow.series_id == series_id,
                TaskRow.user_id == user_id,
                TaskRow.deleted_at.is_(None),
            )
            .order_by(TaskRow.due_at, TaskRow.id)
            .with_for_update()
        )
        return [task_to_entity(row) for row in result.scalars().all()]

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
            bool: ``True`` when another non-deleted task of the series
                occupies the slot.

        Notes:
            - Includes completed tasks because the partial unique index covers
              every non-deleted row.
            - Performs no writes and does not commit.
        """
        statement = select(TaskRow.id).where(
            TaskRow.series_id == series_id,
            TaskRow.user_id == user_id,
            TaskRow.deleted_at.is_(None),
            TaskRow.due_at == due_at,
        )
        if exclude_task_id is not None:
            statement = statement.where(TaskRow.id != exclude_task_id)
        result = await self._session.execute(statement.limit(1))
        return result.scalar_one_or_none() is not None

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
        result = await self._session.execute(
            select(TaskRow.due_at).where(
                TaskRow.series_id == series_id,
                TaskRow.user_id == user_id,
                TaskRow.deleted_at.is_(None),
                TaskRow.due_at >= now,
            )
        )
        return result.scalars().all()


def _new_series_row(series: TaskSeries) -> TaskSeriesRow:
    """
    Build a fresh ORM series row from a domain series.

    Purpose:
        Insert new series without copying the transient domain identifier, so
        PostgreSQL can generate the primary key.

    Args:
        series: Domain series to insert.

    Returns:
        TaskSeriesRow: A transient ORM row populated with persisted fields.

    Notes:
        - The returned row is not added to a session and has no identifier.
    """
    return TaskSeriesRow(
        user_id=series.user_id,
        project_id=series.project_id,
        title=series.title,
        description=series.description,
        priority=series.priority,
        frequency=series.frequency,
        interval_days=series.interval_days,
        time_of_day=series.time_of_day,
        timezone=series.timezone,
        starts_on=series.starts_on,
        ends_on=series.ends_on,
        generated_through=series.generated_through,
        reminders_enabled=series.reminders_enabled,
        deleted_at=series.deleted_at,
        deletion_batch_id=series.deletion_batch_id,
        created_at=series.created_at,
        updated_at=series.updated_at,
    )
