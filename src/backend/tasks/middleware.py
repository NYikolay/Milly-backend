from __future__ import annotations

from typing import Any

import structlog
from taskiq.abc.middleware import TaskiqMiddleware
from taskiq.message import TaskiqMessage
from taskiq.result import TaskiqResult

logger = structlog.get_logger(__name__)


class TaskLoggingMiddleware(TaskiqMiddleware):
    """Propagate request context and log task lifecycle events."""

    def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        """
        Copy the request identifier into the task labels.

        Purpose:
            Carries the caller's request context across the broker boundary.

        Args:
            message: Outgoing task message.

        Returns:
            TaskiqMessage: The message with ``request_id`` copied when present.
        """
        request_id = structlog.contextvars.get_contextvars().get("request_id")
        if request_id:
            message.labels["request_id"] = request_id
        return message

    def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        """
        Bind task identifiers to the structlog context before execution.

        Purpose:
            Makes worker logs correlate with the originating request and task.

        Args:
            message: Incoming task message.

        Returns:
            TaskiqMessage: The unchanged message.
        """
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            task_id=message.task_id,
            task_name=message.task_name,
            request_id=message.labels.get("request_id"),
        )
        return message

    def post_execute(self, message: TaskiqMessage, result: TaskiqResult[Any]) -> None:
        """
        Log task completion and clear the structlog context.

        Purpose:
            Emits one ``task.finished`` event per executed task with its outcome.

        Args:
            message: Executed task message.
            result: Result produced by the task execution.

        Returns:
            None: This hook logs and returns nothing.
        """
        logger.info(
            "task.finished",
            task_id=message.task_id,
            task_name=message.task_name,
            is_err=result.is_err,
            execution_time=result.execution_time,
        )
        structlog.contextvars.clear_contextvars()
