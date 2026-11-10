from __future__ import annotations

from typing import Annotated

from fastapi import Query, Response, status
from pydantic import BaseModel, Field

from backend.api.deps import CurrentAccessDep, ProjectServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.mappers.task_management import project_response
from backend.api.pagination import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from backend.api.queries.projects import (
    encode_project_cursor,
    parse_project_cursor,
    project_filter_fingerprint,
)
from backend.api.task_schemas import (
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
)
from backend.application.task_management.projects import ProjectService
from backend.core.errors import ErrorCode
from backend.domain.task_management.models import ProjectSummary

router = StandardizedAPIRouter(prefix="/projects", tags=["projects"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The project does not exist for the authenticated user.",
    ErrorCode.CONFLICT: "Another active project already uses the requested name.",
    ErrorCode.BAD_REQUEST: "The project name, palette color, or pagination cursor is invalid.",
    ErrorCode.UNAUTHORIZED: "The bearer token is missing or invalid.",
}


class ProjectListQuery(BaseModel):
    """Project-list filters plus pagination parameters accepted by the endpoint."""

    include_archived: bool = False
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    cursor: str | None = None


async def _load_summary(
    service: ProjectService,
    user_id: int,
    project_id: int,
) -> ProjectSummary:
    """
    Load the enriched representation of one project.

    Purpose:
        Combine project fields, progress counters, and color data after a read
        or mutation without loading the project list.

    Args:
        service: Project service resolving summaries.
        user_id: Owner identifier.
        project_id: Project identifier.

    Returns:
        ProjectSummary: The matching summary.

    Raises:
        NotFoundError: If the project does not exist for this user.
    """
    return await service.get_summary(user_id, project_id)


@router.get(
    "",
    response_model=ProjectListResponse,
    responses=document_errors(_ERRORS),
)
async def list_projects(
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
    query: Annotated[ProjectListQuery, Query()],
) -> ProjectListResponse:
    """
    List one page of the authenticated user's projects.

    Purpose:
        Return active projects by default, with progress counters and palette
        colors, and include archived projects on request.

    Args:
        claims: Verified access-token claims.
        service: Project service resolving summaries.
        query: Archive filter plus ``limit`` and ``cursor`` pagination
            parameters.

    Returns:
        ProjectListResponse: Project summaries in
            ``(is_archived, lower(name), id)`` order and the cursor for the
            next page, or ``None`` on the last page.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If a project references a missing color.
        BadRequestError (HTTP 400): If the cursor is malformed, overlong,
            belongs to another user, or was issued for different filters.

    Notes:
        - Archived projects are omitted unless ``include_archived`` is true.
        - The client must resend ``include_archived`` together with ``cursor``.
        - ``limit`` is validated from 1 to 100 with a default of 50.
        - ``cursor`` length is enforced by the cursor decoder so an overlong
          token is a bad request rather than a validation error.
    """
    fingerprint = project_filter_fingerprint(query.include_archived)
    after = (
        parse_project_cursor(
            query.cursor,
            user_id=claims.user_id,
            filter_fingerprint=fingerprint,
        )
        if query.cursor is not None
        else None
    )
    page = await service.list(
        claims.user_id,
        query.include_archived,
        limit=query.limit,
        after=after,
    )
    next_cursor = (
        encode_project_cursor(
            page.next_cursor,
            user_id=claims.user_id,
            filter_fingerprint=fingerprint,
        )
        if page.next_cursor is not None
        else None
    )
    return ProjectListResponse(
        items=[project_response(item) for item in page.items],
        next_cursor=next_cursor,
    )


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    responses=document_errors(_ERRORS),
)
async def create_project(
    payload: ProjectCreate,
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
) -> ProjectResponse:
    """
    Create a project for the authenticated user.

    Purpose:
        Validate the request, persist the project, select the default active
        color when none is provided, and return the enriched representation.

    Args:
        payload: Project creation fields.
        claims: Verified access-token claims.
        service: Project service performing creation.

    Returns:
        ProjectResponse: The created project with color and zero progress.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If the name or palette color is invalid.
        ConflictError (HTTP 409): If another active project uses the name.

    Notes:
        - The request-scoped session commits after the response is built.
    """
    project = await service.create(
        claims.user_id,
        name=payload.name,
        description=payload.description,
        color_id=payload.color_id,
        icon=payload.icon,
    )
    summary = await _load_summary(service, claims.user_id, project.id)
    return project_response(summary)


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
    responses=document_errors(_ERRORS),
)
async def read_project(
    project_id: int,
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
) -> ProjectResponse:
    """
    Return one owned project.

    Purpose:
        Load a project and expose its color and progress counters.

    Args:
        project_id: Project identifier.
        claims: Verified access-token claims.
        service: Project service resolving the project.

    Returns:
        ProjectResponse: The requested project summary.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the project does not exist for this user.

    Notes:
        - Another user's project is reported as not found.
    """
    summary = await _load_summary(service, claims.user_id, project_id)
    return project_response(summary)


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
    responses=document_errors(_ERRORS),
)
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
) -> ProjectResponse:
    """
    Update an owned project.

    Purpose:
        Apply only the provided fields, keep the active name unique, and return
        the refreshed project summary.

    Args:
        project_id: Project identifier.
        payload: Partial project update fields.
        claims: Verified access-token claims.
        service: Project service performing the update.

    Returns:
        ProjectResponse: The updated project summary.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the project does not exist for this user.
        BadRequestError (HTTP 400): If the name or palette color is invalid.
        ConflictError (HTTP 409): If another active project uses the name.

    Notes:
        - Omitted fields keep their current values; an explicit ``null``
          clears description or icon.
    """
    changes = payload.model_dump(exclude_unset=True)
    project = await service.update(claims.user_id, project_id, **changes)
    summary = await _load_summary(service, claims.user_id, project.id)
    return project_response(summary)


@router.post(
    "/{project_id}/archive",
    response_model=ProjectResponse,
    responses=document_errors(_ERRORS),
)
async def archive_project(
    project_id: int,
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
) -> ProjectResponse:
    """
    Archive an owned project.

    Purpose:
        Hide the project from active lists while keeping it available in the
        archive.

    Args:
        project_id: Project identifier.
        claims: Verified access-token claims.
        service: Project service performing the archive.

    Returns:
        ProjectResponse: The archived project summary.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the project does not exist for this user.

    Notes:
        - Archiving an already archived project keeps its original timestamp.
    """
    project = await service.archive(claims.user_id, project_id)
    summary = await _load_summary(service, claims.user_id, project.id)
    return project_response(summary)


@router.post(
    "/{project_id}/unarchive",
    response_model=ProjectResponse,
    responses=document_errors(_ERRORS),
)
async def unarchive_project(
    project_id: int,
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
) -> ProjectResponse:
    """
    Unarchive an owned project.

    Purpose:
        Return an archived project to active lists when its name is still
        available.

    Args:
        project_id: Project identifier.
        claims: Verified access-token claims.
        service: Project service performing the unarchive.

    Returns:
        ProjectResponse: The active project summary.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the project does not exist for this user.
        ConflictError (HTTP 409): If an active project already uses the name.

    Notes:
        - Unarchiving an already active project changes nothing.
    """
    project = await service.unarchive(claims.user_id, project_id)
    summary = await _load_summary(service, claims.user_id, project.id)
    return project_response(summary)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=document_errors(_ERRORS),
)
async def delete_project(
    project_id: int,
    claims: CurrentAccessDep,
    service: ProjectServiceDep,
) -> Response:
    """
    Soft-delete an owned project.

    Purpose:
        Remove the project and its task-management descendants from future
        reads without returning the deleted entity.

    Args:
        project_id: Project identifier.
        claims: Verified access-token claims.
        service: Project service performing the deletion.

    Returns:
        Response: Empty 204 response.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the project does not exist for this user.

    Notes:
        - The deletion is committed by the request-scoped session.
    """
    await service.delete(claims.user_id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
