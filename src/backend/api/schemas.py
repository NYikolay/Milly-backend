from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Readiness response with dependency status."""

    status: str
    database: str


class TaskEnqueuedResponse(BaseModel):
    """Response containing the enqueued task identifier."""

    task_id: str


class ErrorDetail(BaseModel):
    """Machine-readable error payload."""

    code: str
    message: str
    details: object | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """Unified API error envelope."""

    error: ErrorDetail
