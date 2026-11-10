from __future__ import annotations

from backend.api.deps import CurrentAccessDep, PaletteServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.mappers.task_management import color_response
from backend.api.task_schemas import ColorPaletteResponse
from backend.core.errors import ErrorCode

router = StandardizedAPIRouter(prefix="/color-palette", tags=["color-palette"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The requested palette color does not exist.",
    ErrorCode.CONFLICT: "The palette operation conflicts with the active-color rule.",
    ErrorCode.BAD_REQUEST: "The palette request is invalid.",
    ErrorCode.UNAUTHORIZED: "The bearer token is missing or invalid.",
}


@router.get(
    "",
    response_model=ColorPaletteResponse,
    responses=document_errors(_ERRORS),
)
async def read_color_palette(
    claims: CurrentAccessDep,
    service: PaletteServiceDep,
) -> ColorPaletteResponse:
    """
    List selectable palette colors.

    Purpose:
        Expose the active palette in deterministic display order for project
        and label color selection.

    Args:
        claims: Verified access-token claims.
        service: Palette service resolving active colors.

    Returns:
        ColorPaletteResponse: Active colors ordered by ``(sort_order, id)``.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.

    Notes:
        - Inactive colors are never exposed by this endpoint.
    """
    colors = await service.list_active()
    return ColorPaletteResponse(items=[color_response(color) for color in colors])
