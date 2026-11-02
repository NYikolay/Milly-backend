from __future__ import annotations

from fastapi import APIRouter, status

from backend.api.schemas import TaskEnqueuedResponse
from backend.tasks.example import ping

router = APIRouter(prefix="/tasks", tags=["tasks"])


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
        Exception: Propagates broker failures so the unified error handler
            renders an internal error response.

    Notes:
        - Uses the module-level ``ping`` task object, whose broker connection is
          opened lazily by taskiq.
    """
    task = await ping.kiq()
    return TaskEnqueuedResponse(task_id=task.task_id)
