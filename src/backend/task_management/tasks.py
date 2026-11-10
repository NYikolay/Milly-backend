"""Compatibility re-exports of the task application service and DTOs."""

from backend.application.task_management.tasks import TaskService as TaskService
from backend.domain.task_management.models import TaskDetail as TaskDetail
from backend.domain.task_management.models import TaskFilters as TaskFilters
from backend.domain.task_management.models import TaskListCursor as TaskListCursor
from backend.domain.task_management.models import TaskPage as TaskPage

__all__ = ["TaskDetail", "TaskFilters", "TaskListCursor", "TaskPage", "TaskService"]
