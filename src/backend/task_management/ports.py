"""Compatibility re-export of the composite task-management protocol."""

from backend.application.ports.legacy import (
    TaskManagementRepository as TaskManagementRepository,
)

__all__ = ["TaskManagementRepository"]
