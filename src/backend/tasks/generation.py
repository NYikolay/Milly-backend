from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends

from backend.composition.task_management import build_series_generation_service
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
    service = build_series_generation_service(session)
    created = await service.generate(datetime.now(UTC))
    logger.info("task.series_generated", count=created)
