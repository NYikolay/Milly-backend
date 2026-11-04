from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from backend.core.errors import BadRequestError, ConflictError, NotFoundError
from backend.models.color_palette import ColorPalette
from backend.models.project import Project
from backend.task_management.palette import PaletteService
from backend.task_management.ports import TaskManagementRepository

_PROJECT_NOT_FOUND = "Project not found"
_PROJECT_NAME_EXISTS = "Project name already exists"
_PROJECT_NAME_REQUIRED = "Project name is required"
_COLOR_NOT_FOUND = "Palette color not found"


class _UnsetType:
    """Sentinel type for optional update fields that were not provided."""


_UNSET = _UnsetType()


@dataclass(frozen=True)
class ProjectProgress:
    """Task completion counters for one project."""

    total: int
    completed: int
    remaining: int


@dataclass(frozen=True)
class ProjectSummary:
    """A project with its progress and resolved palette color."""

    project: Project
    progress: ProjectProgress
    color: ColorPalette


class ProjectService:
    """Coordinates project lifecycle operations and progress summaries."""

    def __init__(
        self,
        repository: TaskManagementRepository,
        palette_service: PaletteService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with its persistence, palette, and time ports.

        Purpose:
            Keep project rules independent of request handling and make
            timestamps deterministic through an injectable clock.

        Args:
            repository: Persistence operations for projects and progress.
            palette_service: Palette validation and default resolution.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository
        self._palette_service = palette_service
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def list(self, user_id: int, include_archived: bool) -> list[ProjectSummary]:
        """
        List project summaries for a user.

        Purpose:
            Provide active or complete project lists with progress and color
            data for rendering.

        Args:
            user_id: Owner identifier.
            include_archived: Whether archived projects are included.

        Returns:
            list[ProjectSummary]: Project summaries in repository order.

        Raises:
            NotFoundError: If a project references a missing palette color.

        Notes:
            - Performs no writes and does not commit.
        """
        projects = await self._repository.list_projects(
            user_id,
            include_archived=include_archived,
        )
        summaries: list[ProjectSummary] = []
        for project in projects:
            total, completed = await self._repository.project_progress(user_id, project.id)
            color = await self._repository.get_palette_color(project.color_id)
            if color is None:
                raise NotFoundError(_COLOR_NOT_FOUND)
            summaries.append(
                ProjectSummary(
                    project=project,
                    progress=ProjectProgress(
                        total=total,
                        completed=completed,
                        remaining=total - completed,
                    ),
                    color=color,
                )
            )
        return summaries

    async def get(self, user_id: int, project_id: int) -> Project:
        """
        Load one owned project.

        Purpose:
            Resolve a project for a project-detail response.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            Project: The matching non-deleted project.

        Raises:
            NotFoundError: If the project does not exist for this user.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._require_project(user_id, project_id)

    async def create(
        self,
        user_id: int,
        *,
        name: str,
        description: str | None = None,
        color_id: int | None = None,
        icon: str | None = None,
    ) -> Project:
        """
        Create a project.

        Purpose:
            Validate the trimmed name and active color, then persist the
            project in the current unit of work.

        Args:
            user_id: Owner identifier.
            name: Project name; surrounding whitespace is removed.
            description: Optional project description.
            color_id: Palette color identifier; ``None`` selects the default
                active color.
            icon: Optional icon key.

        Returns:
            Project: The flushed project with its generated identifier.

        Raises:
            BadRequestError: If the trimmed name is empty or the color is
                missing or inactive.
            ConflictError: If another active project already uses the name.

        Notes:
            - Flushes through the repository but does not commit.
        """
        normalized_name = self._normalize_name(name)
        if await self._repository.project_name_exists(user_id, normalized_name):
            raise ConflictError(_PROJECT_NAME_EXISTS)
        color = await self._palette_service.resolve_for_write(color_id)
        project = Project(
            user_id=user_id,
            name=normalized_name,
            description=description,
            color_id=color.id,
            icon=icon,
        )
        return await self._repository.add_project(project)

    async def update(
        self,
        user_id: int,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None | _UnsetType = _UNSET,
        color_id: int | None = None,
        icon: str | None | _UnsetType = _UNSET,
    ) -> Project:
        """
        Update a project.

        Purpose:
            Apply name, color, and optional field changes while keeping the
            active name unique.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.
            name: New name; ``None`` keeps the current name. Surrounding
                whitespace is removed.
            description: New description; omit to keep the current value and
                pass ``None`` to clear it.
            color_id: New active palette color; ``None`` keeps the current
                color.
            icon: New icon; omit to keep the current value and pass ``None`` to
                clear it.

        Returns:
            Project: The updated project in the current unit of work.

        Raises:
            NotFoundError: If the project does not exist for this user.
            BadRequestError: If the trimmed name is empty or the color is
                missing or inactive.
            ConflictError: If another active project already uses the name.

        Notes:
            - Locks the project row for the duration of the transaction.
            - Does not commit; the request dependency owns the transaction.
        """
        project = await self._require_project(user_id, project_id, for_update=True)
        if name is not None:
            normalized_name = self._normalize_name(name)
            if await self._repository.project_name_exists(
                user_id,
                normalized_name,
                exclude_project_id=project.id,
            ):
                raise ConflictError(_PROJECT_NAME_EXISTS)
            project.name = normalized_name
        if color_id is not None:
            color = await self._palette_service.ensure_active(color_id)
            project.color_id = color.id
        if not isinstance(description, _UnsetType):
            project.description = description
        if not isinstance(icon, _UnsetType):
            project.icon = icon
        return project

    async def archive(self, user_id: int, project_id: int) -> Project:
        """
        Archive a project.

        Purpose:
            Hide the project and its tasks from active lists while keeping the
            project available in the archive.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            Project: The archived project.

        Raises:
            NotFoundError: If the project does not exist for this user.

        Notes:
            - Archiving an already archived project keeps its original
              timestamp.
            - The first transition to archived clears reminder slots and
              cancels active deliveries for the project's tasks and series.
            - Locks the project row and does not commit.
        """
        project = await self._require_project(user_id, project_id, for_update=True)
        if project.archived_at is None:
            project.archived_at = self._clock()
            await self._repository.pause_project_reminders(user_id, project.id)
        return project

    async def unarchive(self, user_id: int, project_id: int) -> Project:
        """
        Unarchive a project.

        Purpose:
            Return an archived project to active lists when its name is still
            available.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            Project: The active project.

        Raises:
            NotFoundError: If the project does not exist for this user.
            ConflictError: If an active project already uses the name.

        Notes:
            - Unarchiving an already active project changes nothing.
            - The archive-to-active transition recomputes future reminder
              slots from the current time without catching up missed slots.
            - Locks the project row and does not commit.
        """
        project = await self._require_project(user_id, project_id, for_update=True)
        if project.archived_at is None:
            return project
        if await self._repository.project_name_exists(
            user_id,
            project.name,
            exclude_project_id=project.id,
        ):
            raise ConflictError(_PROJECT_NAME_EXISTS)
        project.archived_at = None
        await self._repository.resume_project_reminders(
            user_id,
            project.id,
            self._clock(),
        )
        return project

    async def delete(self, user_id: int, project_id: int) -> None:
        """
        Soft-delete a project and its task-management descendants.

        Purpose:
            Remove a project from all reads and stop generation and reminder
            delivery for its series and tasks in one deletion batch.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.

        Returns:
            None: The deletion stays in the current unit of work.

        Raises:
            NotFoundError: If the project does not exist for this user.

        Notes:
            - Marks the project and asks the repository to soft-delete its
              non-deleted series and tasks and cancel pending deliveries.
            - Locks the project row and does not commit.
        """
        project = await self._require_project(user_id, project_id, for_update=True)
        batch_id = uuid4()
        deleted_at = self._clock()
        await self._repository.soft_delete_project(project, batch_id, deleted_at)

    async def stats(self, user_id: int) -> dict[str, int]:
        """
        Collect dashboard project and plan counts.

        Purpose:
            Provide active project, archived project, and planned task totals
            in one response.

        Args:
            user_id: Owner identifier.

        Returns:
            dict[str, int]: Counts under the ``active``, ``archived``, and
                ``planned`` keys.

        Notes:
            - Performs no writes and does not commit.
        """
        active = await self._repository.count_projects(user_id, archived=False)
        archived = await self._repository.count_projects(user_id, archived=True)
        planned = await self._repository.planned_tasks_count(user_id)
        return {"active": active, "archived": archived, "planned": planned}

    async def _require_project(
        self,
        user_id: int,
        project_id: int,
        *,
        for_update: bool = False,
    ) -> Project:
        """
        Load a project or raise a not-found error.

        Purpose:
            Treat missing projects and another user's projects identically.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Project: The matching non-deleted project.

        Raises:
            NotFoundError: If no project matches the user and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        project = await self._repository.get_project(
            user_id,
            project_id,
            for_update=for_update,
        )
        if project is None:
            raise NotFoundError(_PROJECT_NOT_FOUND)
        return project

    def _normalize_name(self, name: str) -> str:
        """
        Trim a project name and reject an empty result.

        Purpose:
            Apply the shared project name rule before uniqueness checks and
            writes.

        Args:
            name: Candidate project name.

        Returns:
            str: The trimmed name.

        Raises:
            BadRequestError: If the trimmed name is empty.
        """
        normalized = name.strip()
        if not normalized:
            raise BadRequestError(_PROJECT_NAME_REQUIRED)
        return normalized
