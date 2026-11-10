"""Compatibility facade combining every task-management persistence contract."""

from __future__ import annotations

from typing import Protocol

from backend.application.ports.task_management import (
    AccountRepository,
    LabelRepository,
    PaletteRepository,
    ProjectRepository,
    ReminderRepository,
    SeriesRepository,
    TaskRepository,
)
from backend.application.ports.unit_of_work import UnitOfWork


class TaskManagementRepository(
    PaletteRepository,
    ProjectRepository,
    LabelRepository,
    TaskRepository,
    SeriesRepository,
    ReminderRepository,
    AccountRepository,
    UnitOfWork,
    Protocol,
):
    """
    Composite task-management persistence contract kept for compatibility.

    Purpose:
        Let legacy tests and the migration period depend on one repository type
        while new application services consume the aggregate protocols.

    Notes:
        - New application modules must not use this facade.
        - The old ``task_management.ports`` module re-exports it during the
          migration.
    """
