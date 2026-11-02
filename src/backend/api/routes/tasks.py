from __future__ import annotations

from fastapi import status

from backend.api.errors.openapi import StandardizedAPIRouter
from backend.api.schemas import TaskEnqueuedResponse
from backend.tasks.example import ping

router = StandardizedAPIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/ping", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_ping() -> TaskEnqueuedResponse:
    """
    Enqueue the ping task and return its task ID.

    Purpose:
        Expose an on-demand endpoint that proves tasks can be submitted to the
        broker.

    Returns:
        TaskEnqueuedResponse: Identifier of the enqueued ping task.

    Raises:
        TaskiqError: Propagates broker failures so the unified error handler
            renders ``503 task_enqueue_failed``.

    Notes:
        - Uses the module-level ``ping`` task object, whose broker connection is
          opened lazily by taskiq.
    """
    task = await ping.kiq()
    return TaskEnqueuedResponse(task_id=task.task_id)
