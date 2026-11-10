"""Compatibility re-exports of the project application service and DTOs."""

from backend.application.task_management.projects import (
    ProjectService as ProjectService,
)
from backend.domain.task_management.models import ProjectListCursor as ProjectListCursor
from backend.domain.task_management.models import ProjectPage as ProjectPage
from backend.domain.task_management.models import ProjectProgress as ProjectProgress
from backend.domain.task_management.models import ProjectSummary as ProjectSummary

__all__ = [
    "ProjectListCursor",
    "ProjectPage",
    "ProjectProgress",
    "ProjectService",
    "ProjectSummary",
]
