from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query, Response, status

from backend.api.deps import CurrentAccessDep, TaskServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.task_schemas import (
    ReminderResponse,
    TaskCreate,
    TaskFiltersQuery,
    TaskLabelResponse,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
)
from backend.core.errors import ErrorCode
from backend.task_management.enums import Priority
from backend.task_management.tasks import TaskDetail

router = StandardizedAPIRouter(prefix="/tasks", tags=["tasks"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The task, project, label, or series does not exist for the user.",
    ErrorCode.BAD_REQUEST: "A task field or reminder rule is invalid.",
    ErrorCode.UNAUTHORIZED: "The bearer token is missing or invalid.",
}


def _task_response(detail: TaskDetail) -> TaskResponse:
    """
    Build a task response from a service detail.

    Purpose:
        Serialize one task consistently across list and mutation routes.

    Args:
        detail: Task detail returned by the task service.

    Returns:
        TaskResponse: The task with labels and reminders.
    """
    task = detail.task
    return TaskResponse(
        id=task.id,
        series_id=task.series_id,
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        priority=Priority(task.priority),
        due_at=task.due_at,
        completed_at=task.completed_at,
        reminders_enabled=task.reminders_enabled,
        labels=[TaskLabelResponse(id=label.id, name=label.name) for label in detail.labels],
        reminders=[ReminderResponse.from_row(reminder) for reminder in detail.reminders],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _task_changes(payload: TaskUpdate) -> dict[str, Any]:
    """
    Build service update arguments from a partial request.

    Purpose:
        Preserve omitted fields while converting reminder schemas into service
        inputs.

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
    filters: Annotated[TaskFiltersQuery, Query()],
) -> TaskListResponse:
    """
    List the authenticated user's tasks.

    Purpose:
        Return tasks filtered by date, project, priority, status, recurrence,
        and text, including labels and reminders.

    Args:
        claims: Verified access-token claims.
        service: Task service resolving filtered details.
        filters: Query parameters translated into service filters.

    Returns:
        TaskListResponse: Matching tasks in repository order.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        BadRequestError (HTTP 400): If the stored user timezone is invalid.

    Notes:
        - The date filter uses UTC bounds of the user's local day.
    """
    details = await service.list(claims.user_id, filters.to_filters())
    return TaskListResponse(items=[_task_response(detail) for detail in details])


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
    return _task_response(detail)


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
    return _task_response(await service.get(claims.user_id, task_id))


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
        - Future incomplete instances of a series are updated in place.
    """
    detail = await service.update(claims.user_id, task_id, **_task_changes(payload))
    return _task_response(detail)


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
    return _task_response(await service.complete(claims.user_id, task_id))


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
        Clear completion and rebuild reminder slots from the current time.

    Args:
        task_id: Task identifier.
        claims: Verified access-token claims.
        service: Task service performing the reopen.

    Returns:
        TaskResponse: The reopened task detail.

    Raises:
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid.
        NotFoundError (HTTP 404): If the task does not exist for this user.

    Notes:
        - Missed reminder slots are not backfilled.
    """
    return _task_response(await service.uncomplete(claims.user_id, task_id))


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
        Remove a one-off task or apply the series deletion rule for an
        instance without returning the deleted entity.

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
        - The deletion is committed by the request-scoped session.
    """
    await service.delete(claims.user_id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
