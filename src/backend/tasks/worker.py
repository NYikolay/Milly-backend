from __future__ import annotations

from taskiq import TaskiqEvents, TaskiqState

from backend.core.config import get_settings
from backend.core.logging import configure_logging
from backend.db.session import Database
from backend.tasks.broker import broker, scheduler  # noqa: F401
from backend.tasks.delivery import dispatch_task_reminders  # noqa: F401
from backend.tasks.generation import generate_task_series  # noqa: F401

settings = get_settings()
configure_logging(settings)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def startup(state: TaskiqState) -> None:
    """
    Create the worker database engine.

    Purpose:
        Store a process-wide ``Database`` on the broker state so task
        dependencies can open sessions without creating their own engines.

    Args:
        state: Worker state populated by taskiq on startup.

    Returns:
        None: This coroutine only assigns the database.

    Notes:
        - Engine creation is lazy; no database connection is opened here.
        - The database is disposed by the matching shutdown event.
    """
    state.db = Database(settings)


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown(state: TaskiqState) -> None:
    """
    Dispose the worker database engine.

    Purpose:
        Release pooled database connections when the worker stops.

    Args:
        state: Worker state containing the database created on startup.

    Returns:
        None: This coroutine returns after the pool is disposed.

    Notes:
        - Assumes the startup event ran and populated ``state.db``.
    """
    await state.db.dispose()
