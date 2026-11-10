from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query, Response, status

from backend.api.deps import CurrentAccessDep, TaskServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.mappers.task_management import task_list_response, task_response
from backend.api.queries.tasks import (
    encode_task_cursor,
    parse_task_cursor,
    task_filter_fingerprint,
)
from backend.api.task_schemas import (
    TaskCreate,
    TaskListQuery,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
    TodayProgressResponse,
)
from backend.core.errors import ErrorCode

router = StandardizedAPIRouter(prefix="/tasks", tags=["tasks"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The task, project, label, or series does not exist for the user.",
    ErrorCode.BAD_REQUEST: (
        "A task field, reminder rule, pagination cursor, or requested state transition is invalid."
    ),
    ErrorCode.UNAUTHORIZED: "The bearer token is missing or invalid.",
}


def _task_changes(payload: TaskUpdate) -> dict[str, Any]:
    """
    Build service update arguments from a partial request.

    Purpose:
        Preserve omitted fields while converting reminder schemas into service
        inputs; recurrence and interval fields pass through unchanged.

    Args:
        payload: Partial task update fields.

    Returns:
        dict[str, Any]: Keyword arguments accepted by ``TaskService.update``.
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
    response_model=TaskListResponse,
    responses=document_errors(_ERRORS),
)
async def list_tasks(
    claims: CurrentAccessDep,
    service: TaskServiceDep,
    filters: Annotated[TaskListQuery, Query()],
) -> TaskListResponse:
    """
    List one page of the authenticated user's tasks.

    Purpose:
        Return tasks filtered by date, project, priority, status, recurrence,
        and text, including labels and reminders, with a cursor for the next
        page.

    Args:
        claims: Verified access-token claims.
        service: Task service resolving filtered details.
        filters: Task-list filters plus ``limit`` and ``cursor`` pagination
            parameters.

    Returns:
        TaskListResponse: Matching tasks in ``(due_at, id)`` order and the
            cursor for the next page, or ``None`` on the last page.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If the stored user timezone is invalid or
            the cursor is malformed, overlong, belongs to another user, or was
            issued for different filters.

    Notes:
        - The date filter uses UTC bounds of the user's local day.
        - The client must resend the same filters together with ``cursor``.
        - ``limit`` is validated from 1 to 100 with a default of 50.
        - ``cursor`` length is enforced by the cursor decoder so an overlong
          token is a bad request rather than a validation error.
    """
    fingerprint = task_filter_fingerprint(filters)
    after = (
        parse_task_cursor(
            filters.cursor,
            user_id=claims.user_id,
            filter_fingerprint=fingerprint,
        )
        if filters.cursor is not None
        else None
    )
    page = await service.list(
        claims.user_id,
        filters.to_filters(),
        limit=filters.limit,
        after=after,
    )
    next_cursor = (
        encode_task_cursor(
            page.next_cursor,
            user_id=claims.user_id,
            filter_fingerprint=fingerprint,
        )
        if page.next_cursor is not None
        else None
    )
    return task_list_response(page, next_cursor=next_cursor)


@router.get(
    "/today/progress",
    response_model=TodayProgressResponse,
    responses=document_errors(_ERRORS),
)
async def read_today_progress(
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TodayProgressResponse:
    """
    Return the authenticated user's task progress for today.

    Purpose:
        Summarize how many tasks are due in the user's local day and how many
        of them are already completed.

    Args:
        claims: Verified access-token claims.
        service: Task service resolving today's counts.

    Returns:
        TodayProgressResponse: Total, completed, and remaining tasks due today.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If the stored user timezone is invalid.

    Notes:
        - "Today" is the user's local calendar day ``[00:00, next 00:00)``
          converted to UTC; an unset timezone defaults to UTC.
        - ``remaining`` is computed as ``total - completed``.
        - Performs no writes and does not commit.
    """
    total, completed = await service.today_progress(claims.user_id)
    return TodayProgressResponse(
        total=total,
        completed=completed,
        remaining=total - completed,
    )


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses=document_errors(_ERRORS),
)
async def create_task(
    payload: TaskCreate,
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TaskResponse:
    """
    Create a one-off task for the authenticated user.

    Purpose:
        Validate the request, persist the task with its labels and reminder
        rules, and return the computed reminder slots.

    Args:
        payload: Task creation fields.
        claims: Verified access-token claims.
        service: Task service performing creation.

    Returns:
        TaskResponse: The created task with labels and reminders.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If a task field or reminder rule is
            invalid.
        NotFoundError (HTTP 404): If the project or a label does not exist for
            this user.

    Notes:
        - The request-scoped session commits after the response is built.
    """
    detail = await service.create(
        claims.user_id,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        due_at=payload.due_at,
        project_id=payload.project_id,
        label_ids=tuple(payload.label_ids),
        reminders_enabled=payload.reminders_enabled,
        reminders=tuple(reminder.to_input() for reminder in payload.reminders),
    )
    return task_response(detail)


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    responses=document_errors(_ERRORS),
)
async def read_task(
    task_id: int,
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TaskResponse:
    """
    Return one owned task.

    Purpose:
        Load a task together with its labels, reminders, and project data.

    Args:
        task_id: Task identifier.
        claims: Verified access-token claims.
        service: Task service resolving the task.

    Returns:
        TaskResponse: The requested task detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the task does not exist for this user.

    Notes:
        - Another user's task is reported as not found.
    """
    return task_response(await service.get(claims.user_id, task_id))


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    responses=document_errors(_ERRORS),
)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TaskResponse:
    """
    Update an owned task or its recurring series.

    Purpose:
        Apply only the provided fields; a series instance edit is delegated to
        the series rule and returns the representative instance.

    Args:
        task_id: Task identifier.
        payload: Partial task update fields.
        claims: Verified access-token claims.
        service: Task service performing the update.

    Returns:
        TaskResponse: The updated task detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the task, project, label, or series does
            not exist for this user.
        BadRequestError (HTTP 400): If a task field or reminder rule is
            invalid.

    Notes:
        - Omitted fields keep their current values; an explicit ``null``
          clears description or project.
        - ``recurrence`` toggles a task between ``one_off``, ``regular``, and
          ``periodic``; ``periodic`` requires ``interval_days >= 2``.
        - An interval without ``recurrence`` is validated against the target
          series by the service.
        - Future incomplete instances of a series are updated in place.
    """
    detail = await service.update(claims.user_id, task_id, **_task_changes(payload))
    return task_response(detail)


@router.post(
    "/{task_id}/complete",
    response_model=TaskResponse,
    responses=document_errors(_ERRORS),
)
async def complete_task(
    task_id: int,
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TaskResponse:
    """
    Mark an owned task as completed.

    Purpose:
        Record completion and stop pending reminder deliveries for the task.

    Args:
        task_id: Task identifier.
        claims: Verified access-token claims.
        service: Task service performing completion.

    Returns:
        TaskResponse: The completed task detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the task does not exist for this user.

    Notes:
        - Completing a series instance does not affect the series.
    """
    return task_response(await service.complete(claims.user_id, task_id))


@router.post(
    "/{task_id}/uncomplete",
    response_model=TaskResponse,
    responses=document_errors(_ERRORS),
)
async def uncomplete_task(
    task_id: int,
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TaskResponse:
    """
    Reopen an owned task.

    Purpose:
        Clear completion and rebuild reminder slots from the current time,
        recreating a soft-deleted series when a completed series task is
        reopened.

    Args:
        task_id: Task identifier.
        claims: Verified access-token claims.
        service: Task service performing the reopen.

    Returns:
        TaskResponse: The reopened task detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the task does not exist for this user,
            its referenced series is missing, or the project or label of the
            recreated series does not exist for this user.
        BadRequestError (HTTP 400): If the task was completed and its due
            moment is at or before the current instant; or if recreating a
            soft-deleted series finds an invalid stored schedule or timezone,
            an unsupported date range, or an archived project.

    Notes:
        - Missed reminder slots are not backfilled.
        - A completed task whose ``due_at`` is at or before now is rejected
          regardless of its series; only a future due moment reopens.
        - A completed task of a soft-deleted series recreates the series with
          the saved ``frequency`` and ``interval_days``; the task becomes its
          first instance and future instances are generated.
    """
    return task_response(await service.uncomplete(claims.user_id, task_id))


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=document_errors(_ERRORS),
)
async def delete_task(
    task_id: int,
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> Response:
    """
    Delete an owned task or its recurring series.

    Purpose:
        Delete a one-off task individually or remove the whole active series
        without returning the deleted entity.

    Args:
        task_id: Task identifier.
        claims: Verified access-token claims.
        service: Task service performing the deletion.

    Returns:
        Response: Empty 204 response.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the task or its series does not exist for
            this user.

    Notes:
        - Deleting any task of an active series deletes the series, including
          when the selected task is completed or overdue.
        - All future incomplete instances of that series are physically
          deleted.
        - The selected completed or overdue instance is soft-deleted with the
          series; completed and past history is preserved.
        - A one-off task or a task of a soft-deleted series is soft-deleted
          individually with its deliveries cancelled.
        - The deletion is committed by the request-scoped session.
    """
    await service.delete(claims.user_id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
