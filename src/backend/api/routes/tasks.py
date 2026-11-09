from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import Query, Response, status

from backend.api.deps import CurrentAccessDep, TaskServiceDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.pagination import (
    MAX_DB_ID,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)
from backend.api.task_schemas import (
    ReminderResponse,
    TaskCreate,
    TaskFiltersQuery,
    TaskLabelResponse,
    TaskListQuery,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
    TodayProgressResponse,
)
from backend.core.errors import BadRequestError, ErrorCode
from backend.task_management.enums import Priority
from backend.task_management.tasks import TaskDetail, TaskListCursor

router = StandardizedAPIRouter(prefix="/tasks", tags=["tasks"])

_ERRORS = {
    ErrorCode.NOT_FOUND: "The task, project, label, or series does not exist for the user.",
    ErrorCode.BAD_REQUEST: (
        "A task field, reminder rule, pagination cursor, or requested state transition is invalid."
    ),
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
        TaskResponse: The task with labels, reminders, and recurrence mode.
    """
    task = detail.task
    return TaskResponse(
        id=task.id,
        series_id=task.series_id,
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        priority=Priority(task.priority),
        recurrence=detail.recurrence,
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


def _filters_fingerprint(filters: TaskFiltersQuery) -> str:
    """
    Build the canonical fingerprint of task-list filters.

    Purpose:
        Bind a pagination cursor to the exact filter set that produced it.

    Args:
        filters: Validated task-list query parameters.

    Returns:
        str: Stable digest of the canonical filter mapping.

    Notes:
        - ``project_ids`` and ``priorities`` are sorted; ``q`` is unchanged so
          whitespace and casing keep affecting the fingerprint.
    """
    return filter_fingerprint(
        {
            "date": filters.date.isoformat() if filters.date is not None else None,
            "include_archived_projects": filters.include_archived_projects,
            "priorities": sorted(priority.value for priority in filters.priorities),
            "project_ids": sorted(filters.project_ids),
            "q": filters.q,
            "recurrence": filters.recurrence,
            "status": filters.status,
        }
    )


def _task_list_cursor(order_key: list[object]) -> TaskListCursor:
    """
    Convert a decoded cursor ordering key into a typed task cursor.

    Purpose:
        Validate the ordering-key shape and types before querying the next
        page.

    Args:
        order_key: Decoded ordering values from the cursor.

    Returns:
        TaskListCursor: The aware due time and task identifier of the last
            returned task.

    Raises:
        BadRequestError: If the key does not contain exactly an aware ISO-8601
            due time and an integer task identifier within the database range.
    """
    try:
        if len(order_key) != 2:
            raise ValueError
        due_value, task_id = order_key
        if (
            not isinstance(due_value, str)
            or type(task_id) is not int
            or not 1 <= task_id <= MAX_DB_ID
        ):
            raise ValueError
        due_at = datetime.fromisoformat(due_value)
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):  # fmt: skip
        raise BadRequestError("Invalid pagination cursor") from None
    return TaskListCursor(due_at=due_at, task_id=task_id)


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
    fingerprint = _filters_fingerprint(filters)
    after = (
        _task_list_cursor(
            decode_cursor(
                filters.cursor,
                user_id=claims.user_id,
                filter_fingerprint=fingerprint,
            )
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
        encode_cursor(
            user_id=claims.user_id,
            filter_fingerprint=fingerprint,
            order_key=[
                page.next_cursor.due_at.isoformat(),
                page.next_cursor.task_id,
            ],
        )
        if page.next_cursor is not None
        else None
    )
    return TaskListResponse(
        items=[_task_response(detail) for detail in page.items],
        next_cursor=next_cursor,
    )


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
        - ``recurrence`` toggles a task between ``one_off``, ``regular``, and
          ``periodic``; ``periodic`` requires ``interval_days >= 2``.
        - An interval without ``recurrence`` is validated against the target
          series by the service.
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
