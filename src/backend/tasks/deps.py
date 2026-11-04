from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import Context, TaskiqDepends

from backend.db.session import Database


async def get_task_session(
    context: Context = TaskiqDepends(),  # noqa: B008
) -> AsyncGenerator[AsyncSession]:
    """
    Yield a task-scoped session backed by the worker database.

    Purpose:
        Provide task functions with a session bound to the database created
        during worker startup.

    Args:
        context: Taskiq context carrying the worker state.

    Returns:
        AsyncGenerator[AsyncSession]: Session yielded to the task function.

    Notes:
        - ``Database.session()`` commits when the task body exits normally and
          rolls back when it raises; the session is closed in both cases.
        - The dependency does not create the database; the worker startup event
          stores it in ``context.state.db``.
    """
    database: Database = context.state.db
    async with database.session() as session:
        yield session
