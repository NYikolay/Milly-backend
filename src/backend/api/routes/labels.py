from __future__ import annotations

from fastapi import Response, status

from backend.api.deps import CurrentAccessDep, LabelServiceDep, PaletteServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.mappers.task_management import label_response
from backend.api.task_schemas import (
    LabelCreate,
    LabelListResponse,
    LabelResponse,
    LabelUpdate,
)
from backend.core.errors import ErrorCode

router = StandardizedAPIRouter(prefix="/labels", tags=["labels"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The label or its palette color does not exist.",
    ErrorCode.CONFLICT: "Another label already uses the requested name.",
    ErrorCode.BAD_REQUEST: "The label name or palette color is invalid.",
    ErrorCode.UNAUTHORIZED: "The bearer token is missing or invalid.",
}


@router.get(
    "",
    response_model=LabelListResponse,
    responses=document_errors(_ERRORS),
)
async def list_labels(
    claims: CurrentAccessDep,
    service: LabelServiceDep,
    palette_service: PaletteServiceDep,
) -> LabelListResponse:
    """
    List the authenticated user's labels.

    Purpose:
        Return owned labels with their stored palette colors for selection and
        management.

    Args:
        claims: Verified access-token claims.
        service: Label service listing owned labels.
        palette_service: Palette service resolving label colors.

    Returns:
        LabelListResponse: Labels in repository order.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If a label references a missing color.

    Notes:
        - Distinct colors are resolved in one batched palette lookup per
          request.
    """
    labels = await service.list(claims.user_id)
    color_ids = sorted({label.color_id for label in labels})
    colors = await palette_service.get_colors(color_ids)
    return LabelListResponse(
        items=[label_response(label, colors[label.color_id]) for label in labels]
    )


@router.post(
    "",
    response_model=LabelResponse,
    status_code=status.HTTP_201_CREATED,
    responses=document_errors(_ERRORS),
)
async def create_label(
    payload: LabelCreate,
    claims: CurrentAccessDep,
    service: LabelServiceDep,
    palette_service: PaletteServiceDep,
) -> LabelResponse:
    """
    Create a label for the authenticated user.

    Purpose:
        Validate the request, persist the label, select the default active
        color when none is provided, and return the stored color.

    Args:
        payload: Label creation fields.
        claims: Verified access-token claims.
        service: Label service performing creation.
        palette_service: Palette service resolving the stored color.

    Returns:
        LabelResponse: The created label with its color.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If the name or palette color is invalid.
        ConflictError (HTTP 409): If another label already uses the name.

    Notes:
        - The request-scoped session commits after the response is built.
    """
    label = await service.create(
        claims.user_id,
        name=payload.name,
        color_id=payload.color_id,
    )
    color = await palette_service.get_color(label.color_id)
    return label_response(label, color)


@router.patch(
    "/{label_id}",
    response_model=LabelResponse,
    responses=document_errors(_ERRORS),
)
async def update_label(
    label_id: int,
    payload: LabelUpdate,
    claims: CurrentAccessDep,
    service: LabelServiceDep,
    palette_service: PaletteServiceDep,
) -> LabelResponse:
    """
    Update an owned label.

    Purpose:
        Apply only the provided fields, keep label names unique, and return the
        refreshed label with its stored color.

    Args:
        label_id: Label identifier.
        payload: Partial label update fields.
        claims: Verified access-token claims.
        service: Label service performing the update.
        palette_service: Palette service resolving the stored color.

    Returns:
        LabelResponse: The updated label with its color.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the label does not exist for this user.
        BadRequestError (HTTP 400): If the name or palette color is invalid.
        ConflictError (HTTP 409): If another label already uses the name.

    Notes:
        - Omitted fields keep their current values.
    """
    changes = payload.model_dump(exclude_unset=True)
    label = await service.update(claims.user_id, label_id, **changes)
    color = await palette_service.get_color(label.color_id)
    return label_response(label, color)


@router.delete(
    "/{label_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=document_errors(_ERRORS),
)
async def delete_label(
    label_id: int,
    claims: CurrentAccessDep,
    service: LabelServiceDep,
) -> Response:
    """
    Soft-delete an owned label.

    Purpose:
        Hide the label from selection and responses without returning the
        deleted entity.

    Args:
        label_id: Label identifier.
        claims: Verified access-token claims.
        service: Label service performing the deletion.

    Returns:
        Response: Empty 204 response.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.

    Notes:
        - Deletion is idempotent; a missing or already deleted label still
          returns 204.
    """
    await service.delete(claims.user_id, label_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
