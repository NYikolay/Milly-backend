from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends

from backend.core.config import get_settings
from backend.task_management.reminders import ReminderDispatcher
from backend.task_management.repository import SqlAlchemyTaskManagementRepository
from backend.tasks.broker import broker
from backend.tasks.deps import get_task_session
from backend.tasks.telegram import TelegramBotSender

logger = structlog.get_logger(__name__)


@broker.task(
    task_name="dispatch_task_reminders",
    schedule=[{"cron": "* * * * *", "schedule_id": "reminder-delivery"}],
)
async def dispatch_task_reminders(
    session: AsyncSession = TaskiqDepends(get_task_session),  # noqa: B008
) -> None:
    """
    Deliver every reminder that is due in the current minute.

    Purpose:
        Run the scheduled Telegram reminder-delivery job inside the worker.

    Args:
        session: Task-scoped database session provided by taskiq.

    Returns:
        None: Delivery state is committed by the dispatcher and session
            dependency.

    Notes:
        - The label schedule runs the job every minute.
        - The bot token is read from application settings when the job starts.
        - Only the number of successful deliveries is logged.
    """
    repository = SqlAlchemyTaskManagementRepository(session)
    dispatcher = ReminderDispatcher(
        repository,
        TelegramBotSender(get_settings()),
        clock=lambda: datetime.now(UTC),
    )
    sent = await dispatcher.dispatch_due()
    logger.info("task.reminders_dispatched", count=sent)
