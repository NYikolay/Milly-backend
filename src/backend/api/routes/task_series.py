from __future__ import annotations

from typing import Any

from fastapi import Response, status

from backend.api.deps import CurrentAccessDep, SeriesServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.task_schemas import (
    ReminderResponse,
    SeriesCreate,
    SeriesListResponse,
    SeriesResponse,
    SeriesUpdate,
    TaskLabelResponse,
)
from backend.core.errors import ErrorCode
from backend.task_management.enums import Priority, SeriesFrequency
from backend.task_management.series import SeriesDetail

router = StandardizedAPIRouter(prefix="/task-series", tags=["task-series"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The series, project, or label does not exist for the user.",
    ErrorCode.BAD_REQUEST: "A series field or reminder rule is invalid.",
    ErrorCode.UNAUTHORIZED: "The bearer token is missing or invalid.",
}


def _series_response(detail: SeriesDetail) -> SeriesResponse:
    """
    Build a series response from a service detail.

    Purpose:
        Serialize one recurring series with its schedule, labels, and template
        reminders.

    Args:
        detail: Series detail returned by the series service.

    Returns:
        SeriesResponse: The series fields exposed to clients, including its
            frequency and interval between occurrences.
    """
    series = detail.series
    return SeriesResponse(
        id=series.id,
        title=series.title,
        description=series.description,
        priority=Priority(series.priority),
        frequency=SeriesFrequency(series.frequency),
        interval_days=series.interval_days,
        time_of_day=series.time_of_day,
        timezone=series.timezone,
        starts_on=series.starts_on,
        ends_on=series.ends_on,
        generated_through=series.generated_through,
        reminders_enabled=series.reminders_enabled,
        labels=[TaskLabelResponse(id=label.id, name=label.name) for label in detail.labels],
        reminders=[ReminderResponse.from_row(reminder) for reminder in detail.reminders],
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


def _series_changes(payload: SeriesUpdate) -> dict[str, Any]:
    """
    Build service update arguments from a partial request.

    Purpose:
        Preserve omitted fields while converting reminder schemas into service
        inputs.

    Args:
        payload: Partial series update fields.

    Returns:
        dict[str, Any]: Keyword arguments accepted by
            ``SeriesService.update``.
    """
    changes = payload.model_dump(exclude_unset=True)
    if "reminders" in changes:
        reminders = payload.reminders
        changes["reminders"] = (
            None if reminders is None else tuple(reminder.to_input() for reminder in reminders)
        )
    return changes


@router.get(
    "",
    response_model=SeriesListResponse,
    responses=document_errors(_ERRORS),
)
async def list_series(
    claims: CurrentAccessDep,
    service: SeriesServiceDep,
) -> SeriesListResponse:
    """
    List the authenticated user's recurring series.

    Purpose:
        Return every non-deleted series with its schedule, labels, and template
        reminders.

    Args:
        claims: Verified access-token claims.
        service: Series service resolving details.

    Returns:
        SeriesListResponse: Series details in repository order.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.

    Notes:
        - Performs no writes.
    """
    details = await service.list_details(claims.user_id)
    return SeriesListResponse(items=[_series_response(detail) for detail in details])


@router.post(
    "",
    response_model=SeriesResponse,
    status_code=status.HTTP_201_CREATED,
    responses=document_errors(_ERRORS),
)
async def create_series(
    payload: SeriesCreate,
    claims: CurrentAccessDep,
    service: SeriesServiceDep,
) -> SeriesResponse:
    """
    Create a daily or periodic recurring series for the authenticated user.

    Purpose:
        Validate the request, persist a daily or periodic series template,
        generate its first instances, and return the schedule and generation
        metadata.

    Args:
        payload: Series creation fields, including frequency and interval.
        claims: Verified access-token claims.
        service: Series service performing creation and generation.

    Returns:
        SeriesResponse: The created series with labels and reminders.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If a series field or reminder rule is
            invalid.
        NotFoundError (HTTP 404): If the project or a label does not exist for
            this user.

    Notes:
        - Daily series default to an interval of one day; periodic series
          require ``interval_days >= 2``.
        - The request-scoped session commits after the response is built.
        - ``generated_through`` reflects the last generated occurrence.
    """
    series = await service.create(
        claims.user_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        frequency=payload.frequency,
        interval_days=payload.interval_days,
        time_of_day=payload.time_of_day,
        timezone=payload.timezone,
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        project_id=payload.project_id,
        label_ids=tuple(payload.label_ids),
        reminders_enabled=payload.reminders_enabled,
        reminders=tuple(reminder.to_input() for reminder in payload.reminders),
    )
    return _series_response(await service.get_detail(claims.user_id, series.id))


@router.get(
    "/{series_id}",
    response_model=SeriesResponse,
    responses=document_errors(_ERRORS),
)
async def read_series(
    series_id: int,
    claims: CurrentAccessDep,
    service: SeriesServiceDep,
) -> SeriesResponse:
    """
    Return one owned recurring series.

    Purpose:
        Load a series with its schedule, labels, and template reminders.

    Args:
        series_id: Series identifier.
        claims: Verified access-token claims.
        service: Series service resolving the detail.

    Returns:
        SeriesResponse: The requested series detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the series does not exist for this user.

    Notes:
        - Another user's series is reported as not found.
    """
    return _series_response(await service.get_detail(claims.user_id, series_id))


@router.patch(
    "/{series_id}",
    response_model=SeriesResponse,
    responses=document_errors(_ERRORS),
)
async def update_series(
    series_id: int,
    payload: SeriesUpdate,
    claims: CurrentAccessDep,
    service: SeriesServiceDep,
) -> SeriesResponse:
    """
    Update an owned recurring series.

    Purpose:
        Apply only the provided fields, regenerate future instances when the
        frequency, interval, or other schedule fields change, and return the
        refreshed series.

    Args:
        series_id: Series identifier.
        payload: Partial series update fields.
        claims: Verified access-token claims.
        service: Series service performing the update.

    Returns:
        SeriesResponse: The updated series detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the series, project, or label does not
            exist for this user.
        BadRequestError (HTTP 400): If a series field or reminder rule is
            invalid.

    Notes:
        - Past and completed instances keep their snapshots.
        - An explicit ``null`` clears description, end date, or project.
        - Changing ``frequency`` or ``interval_days`` regenerates future
          incomplete instances; periodic series require ``interval_days >= 2``.
        - An interval without ``frequency`` is validated against the stored
          series by the service.
    """
    await service.update(claims.user_id, series_id, **_series_changes(payload))
    return _series_response(await service.get_detail(claims.user_id, series_id))


@router.delete(
    "/{series_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=document_errors(_ERRORS),
)
async def delete_series(
    series_id: int,
    claims: CurrentAccessDep,
    service: SeriesServiceDep,
) -> Response:
    """
    Delete an owned recurring series.

    Purpose:
        Stop generation and delivery while preserving past task history.

    Args:
        series_id: Series identifier.
        claims: Verified access-token claims.
        service: Series service performing the deletion.

    Returns:
        Response: Empty 204 response.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the series does not exist for this user.

    Notes:
        - The deletion is committed by the request-scoped session.
    """
    await service.delete(claims.user_id, series_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
