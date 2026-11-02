from __future__ import annotations

from datetime import UTC, datetime

import structlog

from backend.tasks.broker import broker

logger = structlog.get_logger(__name__)


@broker.task(task_name="ping")
async def ping() -> dict[str, str]:
    """
    Return a timestamp to prove the worker is executing tasks.

    Purpose:
        Serves as a minimal on-demand task for smoke-testing the worker.

    Returns:
        dict[str, str]: Static status plus the current UTC time in ISO format.
    """
    logger.info("task.ping")
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@broker.task(
    task_name="heartbeat",
    schedule=[{"cron": "*/5 * * * *", "schedule_id": "heartbeat"}],
)
async def heartbeat() -> None:
    """
    Emit a periodic log entry to demonstrate the scheduler.

    Purpose:
        Runs every five minutes through the label schedule source.

    Returns:
        None: The task only writes a log record.
    """
    logger.info("task.heartbeat")
