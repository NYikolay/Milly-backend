from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Readiness response with dependency status."""

    status: str
    database: str


class TaskEnqueuedResponse(BaseModel):
    """Response containing the enqueued task identifier."""

    task_id: str
