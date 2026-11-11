"""Composition factories for task-management application services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.ports.collaborators import ReminderSender
from backend.application.task_management.labels import LabelService
from backend.application.task_management.palette import PaletteService
from backend.application.task_management.projects import ProjectService
from backend.application.task_management.reminders import ReminderDispatcher
from backend.application.task_management.series import SeriesService
from backend.application.task_management.series_generation import (
    SeriesGenerationService,
)
from backend.application.task_management.series_materializer import SeriesMaterializer
from backend.application.task_management.task_details import TaskDetails
from backend.application.task_management.task_mutations import TaskMutationService
from backend.application.task_management.task_references import TaskReferences
from backend.application.task_management.tasks import TaskService
from backend.infrastructure.persistence.sqlalchemy.account_repository import (
    SqlAlchemyAccountRepository,
)
from backend.infrastructure.persistence.sqlalchemy.label_repository import (
    SqlAlchemyLabelRepository,
)
from backend.infrastructure.persistence.sqlalchemy.palette_repository import (
    SqlAlchemyPaletteRepository,
)
from backend.infrastructure.persistence.sqlalchemy.project_repository import (
    SqlAlchemyProjectRepository,
)
from backend.infrastructure.persistence.sqlalchemy.reminder_repository import (
    SqlAlchemyReminderRepository,
)
from backend.infrastructure.persistence.sqlalchemy.series_repository import (
    SqlAlchemySeriesRepository,
)
from backend.infrastructure.persistence.sqlalchemy.task_repository import (
    SqlAlchemyTaskRepository,
)
from backend.infrastructure.persistence.sqlalchemy.unit_of_work import (
    SqlAlchemyUnitOfWork,
)

__all__ = [
    "TaskManagementServices",
    "build_palette_service",
    "build_reminder_dispatcher",
    "build_series_generation_service",
    "build_series_service",
    "build_task_management_services",
]

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]


@dataclass(frozen=True)
class TaskManagementServices:
    """Application services built over one session and one unit of work."""

    palette: PaletteService
    projects: ProjectService
    labels: LabelService
    series: SeriesService
    tasks: TaskService
    task_mutations: TaskMutationService


def build_task_management_services(
    session: AsyncSession,
    *,
    clock: Clock | None = None,
) -> TaskManagementServices:
    """
    Build the complete task-management service graph for one session.

    Purpose:
        Give API and worker entrypoints one place that selects concrete
        adapters and shares one unit of work across every service.

    Args:
        session: Request- or job-scoped async session owning the transaction.
        clock: Current-time provider. Defaults to the service UTC clock.

    Returns:
        TaskManagementServices: Palette, project, label, series, task, and
            task-mutation services wired over the supplied session.

    Notes:
        - Every aggregate adapter and the unit of work share the same session.
        - The task services receive the aggregate project repository as their
          project reader so the today-progress aggregate is available.
        - Task details and reference validation are shared between the task
          query/creation and task-mutation services.
        - Does not commit, flush, or open a transaction.
    """
    palette_repository = SqlAlchemyPaletteRepository(session)
    account_repository = SqlAlchemyAccountRepository(session)
    project_repository = SqlAlchemyProjectRepository(session)
    label_repository = SqlAlchemyLabelRepository(session)
    task_repository = SqlAlchemyTaskRepository(session)
    series_repository = SqlAlchemySeriesRepository(session)
    reminder_repository = SqlAlchemyReminderRepository(session)
    unit_of_work = SqlAlchemyUnitOfWork(session)

    palette = PaletteService(palette_repository)
    projects = ProjectService(
        project_repository,
        palette,
        reminder_repository,
        clock=clock,
    )
    labels = LabelService(label_repository, palette, clock=clock)
    references = TaskReferences(project_repository, label_repository, account_repository)
    details = TaskDetails(task_repository, series_repository, project_repository)
    materializer = SeriesMaterializer(
        series_repository,
        task_repository,
        reminder_repository,
        project_repository,
        references,
        unit_of_work,
    )
    series = SeriesService(
        series_repository,
        task_repository,
        reminder_repository,
        references,
        materializer,
        unit_of_work,
        clock=clock,
    )
    tasks = TaskService(
        task_repository,
        reminder_repository,
        project_repository,
        references,
        details,
        unit_of_work,
        clock=clock,
    )
    task_mutations = TaskMutationService(
        task_repository,
        series_repository,
        reminder_repository,
        references,
        details,
        series,
        unit_of_work,
        clock=clock,
    )
    return TaskManagementServices(
        palette=palette,
        projects=projects,
        labels=labels,
        series=series,
        tasks=tasks,
        task_mutations=task_mutations,
    )


def build_series_service(
    session: AsyncSession,
    *,
    clock: Clock | None = None,
) -> SeriesService:
    """
    Build the recurring-series service for API request handling.

    Purpose:
        Give the API series entrypoint one factory that selects the aggregate
        adapters and shared unit of work.

    Args:
        session: Request-scoped async session owning the transaction.
        clock: Current-time provider. Defaults to the service UTC clock.

    Returns:
        SeriesService: Series service wired over the supplied session.

    Notes:
        - Series, task, reminder, account, project, and label adapters, the
          materializer, and the unit of work all share the same session.
        - Does not commit, flush, or open a transaction.
    """
    account_repository = SqlAlchemyAccountRepository(session)
    project_repository = SqlAlchemyProjectRepository(session)
    label_repository = SqlAlchemyLabelRepository(session)
    series_repository = SqlAlchemySeriesRepository(session)
    task_repository = SqlAlchemyTaskRepository(session)
    reminder_repository = SqlAlchemyReminderRepository(session)
    references = TaskReferences(project_repository, label_repository, account_repository)
    unit_of_work = SqlAlchemyUnitOfWork(session)
    materializer = SeriesMaterializer(
        series_repository,
        task_repository,
        reminder_repository,
        project_repository,
        references,
        unit_of_work,
    )
    return SeriesService(
        series_repository,
        task_repository,
        reminder_repository,
        references,
        materializer,
        unit_of_work,
        clock=clock,
    )


def build_series_generation_service(
    session: AsyncSession,
    *,
    clock: Clock | None = None,
) -> SeriesGenerationService:
    """
    Build the scheduled series-generation service for a Taskiq job.

    Purpose:
        Give the generation entrypoint one factory that selects the aggregate
        adapters and shared unit of work.

    Args:
        session: Job-scoped async session owning the transaction.
        clock: Current-time provider. Defaults to the service UTC clock.

    Returns:
        SeriesGenerationService: Generator wired over the supplied session.

    Notes:
        - Series, task, reminder, account, project, and label adapters, the
          materializer, and the unit of work all share the same session.
        - The materializer owns the active-project gate; the generator needs no
          separate project reader.
        - Does not commit, flush, or open a transaction.
    """
    account_repository = SqlAlchemyAccountRepository(session)
    project_repository = SqlAlchemyProjectRepository(session)
    label_repository = SqlAlchemyLabelRepository(session)
    series_repository = SqlAlchemySeriesRepository(session)
    task_repository = SqlAlchemyTaskRepository(session)
    reminder_repository = SqlAlchemyReminderRepository(session)
    references = TaskReferences(project_repository, label_repository, account_repository)
    unit_of_work = SqlAlchemyUnitOfWork(session)
    materializer = SeriesMaterializer(
        series_repository,
        task_repository,
        reminder_repository,
        project_repository,
        references,
        unit_of_work,
    )
    return SeriesGenerationService(
        series_repository,
        materializer,
        unit_of_work,
        clock=clock,
    )


def build_palette_service(session: AsyncSession) -> PaletteService:
    """
    Build a palette service for admin and API consumers.

    Purpose:
        Select the palette adapter while binding the service to the caller's
        session.

    Args:
        session: Session that owns the palette reads and policy checks.

    Returns:
        PaletteService: Palette service wired over the supplied session.

    Notes:
        - The service never commits; the caller owns the transaction boundary.
    """
    return PaletteService(SqlAlchemyPaletteRepository(session))


def build_reminder_dispatcher(
    session: AsyncSession,
    sender: ReminderSender,
    *,
    clock: Clock | None = None,
    monotonic: Monotonic | None = None,
) -> ReminderDispatcher:
    """
    Build the reminder dispatcher for a Taskiq delivery job.

    Purpose:
        Give the delivery entrypoint one factory that selects the aggregate
        readers, the shared unit of work, and the configured sender.

    Args:
        session: Job-scoped async session owning the transaction.
        sender: Telegram-like message sender.
        clock: Current-time provider. Defaults to the dispatcher UTC clock.
        monotonic: Monotonic time provider used for the dispatch deadline.
            Defaults to ``time.monotonic``.

    Returns:
        ReminderDispatcher: Dispatcher wired over the supplied session.

    Notes:
        - Reminder, task, series, project, and account adapters and the unit of
          work all share the same session.
        - Does not commit, flush, or open a transaction.
    """
    return ReminderDispatcher(
        SqlAlchemyReminderRepository(session),
        SqlAlchemyTaskRepository(session),
        SqlAlchemySeriesRepository(session),
        SqlAlchemyProjectRepository(session),
        SqlAlchemyAccountRepository(session),
        SqlAlchemyUnitOfWork(session),
        sender,
        clock=clock,
        monotonic=monotonic,
    )
