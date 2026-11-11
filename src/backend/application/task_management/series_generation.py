from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog

from backend.application.ports.task_management import SeriesRepository
from backend.application.ports.unit_of_work import UnitOfWork
from backend.application.task_management.series_materializer import SeriesMaterializer
from backend.domain.task_management.policies import HORIZON_DAYS

logger = structlog.get_logger(__name__)

__all__ = ["SeriesGenerationService"]


class SeriesGenerationService:
    """Generates missing instances for every eligible recurring series."""

    def __init__(
        self,
        series_repository: SeriesRepository,
        materializer: SeriesMaterializer,
        unit_of_work: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the generator with persistence, materialization, and time ports.

        Purpose:
            Run the scheduled generation job over one series at a time while
            keeping the transaction boundary under the caller's control.

        Args:
            series_repository: Persistence reads, selection, and row claims for
                series templates.
            materializer: Occurrence generation for a single series.
            unit_of_work: Transaction boundary used per series.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - ``generate`` receives the run instant explicitly; the clock keeps
              the constructor consistent with the other application services.
        """
        self._series_repository = series_repository
        self._materializer = materializer
        self._unit_of_work = unit_of_work
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

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
            - Commits through the unit of work after each series so a later
              failure does not roll back earlier successful work and row locks
              are released promptly.
            - Candidate ids are snapshotted before processing so rollback does
              not require touching expired entities.
            - Unexpected per-series errors roll back and continue with the next
              series.
            - Per-series materialization refuses archived or deleted projects.
            - Idempotent for slots already present in the repository.
            - Sets ``generated_through`` to the last generated due time; it only
              falls back to ``now`` while the series has no generation marker
              yet.
        """
        created = 0
        horizon = now + timedelta(days=HORIZON_DAYS)
        candidates = [
            (series.id, series.user_id)
            for series in await self._series_repository.list_series_for_generation(now)
        ]
        for series_id, series_user_id in candidates:
            try:
                series = await self._series_repository.claim_series_for_generation(
                    series_id,
                    series_user_id,
                )
                if series is None:
                    await self._unit_of_work.commit()
                    continue
                if series.generated_through is not None and series.generated_through >= horizon:
                    await self._unit_of_work.commit()
                    continue
                created += await self._materializer.materialize(series, now)
                await self._unit_of_work.commit()
            except Exception as error:
                await self._unit_of_work.rollback()
                logger.warning(
                    "task.series_generation_failed",
                    series_id=series_id,
                    error=type(error).__name__,
                )
        return created
