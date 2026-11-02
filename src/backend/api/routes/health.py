from __future__ import annotations

from backend.api.deps import DatabaseProbeDep
from backend.api.errors.openapi import StandardizedAPIRouter
from backend.api.schemas import HealthResponse
from backend.core.errors import AppError, ErrorCode

router = StandardizedAPIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """
    Report process liveness.

    Purpose:
        Expose a dependency-free probe that confirms the API process is
        serving requests.

    Returns:
        dict[str, str]: ``{"status": "ok"}``.

    Notes:
        - Does not touch the database or any external service.
    """
    return {"status": "ok"}


@router.get("/ready", response_model=HealthResponse)
async def ready(probe: DatabaseProbeDep) -> HealthResponse:
    """
    Report readiness by probing the database.

    Purpose:
        Expose a probe that reports whether the API can reach its database.

    Args:
        probe: Database readiness probe bound to the application engine.

    Returns:
        HealthResponse: Healthy status with ``database="ok"``.

    Raises:
        AppError: With ``DATABASE_UNAVAILABLE`` when the probe fails; rendered
            as the unified HTTP 503 error envelope.

    Notes:
        - Returns HTTP 200 when the probe succeeds and the unified 503 error
          envelope otherwise.
        - The probe itself bounds the database check with a timeout.
    """
    if await probe.check():
        return HealthResponse(status="ok", database="ok")
    raise AppError(
        ErrorCode.DATABASE_UNAVAILABLE,
        message="Database is unavailable",
        details={"database": "down"},
    )
