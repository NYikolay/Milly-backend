from __future__ import annotations

from fastapi import APIRouter, Response, status

from backend.api.deps import DatabaseProbeDep
from backend.api.schemas import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


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


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def ready(response: Response, probe: DatabaseProbeDep) -> HealthResponse:
    """
    Report readiness by probing the database.

    Purpose:
        Expose a probe that reports whether the API can reach its database.

    Args:
        response: Response object used to set a 503 status when unavailable.
        probe: Database readiness probe bound to the application engine.

    Returns:
        HealthResponse: Healthy status with ``database="ok"`` or unavailable
            status with ``database="down"``.

    Notes:
        - Returns HTTP 200 when the probe succeeds and HTTP 503 otherwise.
        - The probe itself bounds the database check with a timeout.
    """
    if await probe.check():
        return HealthResponse(status="ok", database="ok")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="unavailable", database="down")
