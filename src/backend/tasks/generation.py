from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends

from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.repository import SqlAlchemyTaskManagementRepository
from backend.task_management.series import SeriesService
from backend.tasks.broker import broker
from backend.tasks.deps import get_task_session

logger = structlog.get_logger(__name__)


@broker.task(
    task_name="generate_task_series",
    schedule=[{"cron": "*/15 * * * *", "schedule_id": "task-series-generation"}],
)
async def generate_task_series(
    session: AsyncSession = TaskiqDepends(get_task_session),  # noqa: B008
) -> None:
    """
    Generate upcoming instances for every eligible task series.

    Purpose:
        Run the scheduled recurring-task materialization job inside the worker.

    Args:
        session: Task-scoped database session provided by taskiq.

    Returns:
        None: Generated instances are committed by the session dependency.

    Notes:
        - The label schedule runs the job every fifteen minutes.
        - Concurrent workers skip series that are locked by another run.
        - Only the number of created instances is logged.
    """
    repository = SqlAlchemyTaskManagementRepository(session)
    palette_service = PaletteService(repository)
    service = SeriesService(
        repository,
        ProjectService(repository, palette_service),
        LabelService(repository, palette_service),
    )
    created = await service.generate(datetime.now(UTC))
    logger.info("task.series_generated", count=created)
