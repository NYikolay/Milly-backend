from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from backend.api.pagination import DEFAULT_PAGE_SIZE
from backend.application.ports.collaborators import ProjectReminderCommands
from backend.application.ports.task_management import (
    PaletteRepository,
    ProjectRepository,
)
from backend.application.task_management.palette import PaletteService
from backend.application.task_management.project_reminders import ProjectReminderService
from backend.application.task_management.projects import ProjectService
from backend.core.errors import BadRequestError, ConflictError, ErrorCode, NotFoundError
from backend.domain.entities import ColorPalette, Project
from backend.domain.task_management.models import (
    ProjectListCursor,
    ProjectPage,
    ProjectProgress,
    ProjectSummary,
)
from tests.task_management_fakes import FakeTaskManagementRepository, fixed_now

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
USER_ID = 7


def clock() -> datetime:
    """Return the fixed service clock."""
    return NOW


def make_repository() -> AsyncMock:
    """Build an async repository double constrained to the project port."""
    return AsyncMock(spec=ProjectRepository)


def make_palette_repository() -> AsyncMock:
    """Build an async repository double constrained to the palette port."""
    return AsyncMock(spec=PaletteRepository)


def make_reminder_repository() -> AsyncMock:
    """Build an async double constrained to the project reminder commands."""
    return AsyncMock(spec=ProjectReminderCommands)


def make_color(*, color_id: int = 1) -> ColorPalette:
    """Build a palette color with deterministic values."""
    return ColorPalette(
        id=color_id,
        slug=f"color-{color_id}",
        name=f"Color {color_id}",
        hex="#000000",
        sort_order=color_id,
        is_active=True,
    )


def make_project(
    *,
    project_id: int = 1,
    name: str = "Work",
    color_id: int = 1,
    archived_at: datetime | None = None,
) -> Project:
    """Build a user project with deterministic values."""
    return Project(
        id=project_id,
        user_id=USER_ID,
        name=name,
        description=None,
        color_id=color_id,
        icon=None,
        archived_at=archived_at,
        deleted_at=None,
        deletion_batch_id=None,
    )


def make_service(
    repository: AsyncMock,
    palette_repository: AsyncMock | None = None,
    reminder_repository: AsyncMock | None = None,
) -> ProjectService:
    """Build a project service with the fixed clock and real palette rules."""
    palette = palette_repository if palette_repository is not None else make_palette_repository()
    reminders = (
        reminder_repository if reminder_repository is not None else make_reminder_repository()
    )
    return ProjectService(repository, PaletteService(palette), reminders, clock=clock)


async def _return_project(project: Project) -> Project:
    """Return the project passed to the repository double."""
    return project


async def test_list_returns_progress_and_palette_color() -> None:
    """Project summaries include task progress and the assigned palette color."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    project = make_project()
    color = make_color()
    repository.list_projects.return_value = [(project, "work")]
    repository.project_progress_for_projects.return_value = {project.id: (5, 2)}
    palette_repository.get_palette_colors_by_ids.return_value = {color.id: color}

    service = make_service(repository, palette_repository)

    result = await service.list(
        USER_ID,
        include_archived=False,
        limit=DEFAULT_PAGE_SIZE,
    )

    assert result == ProjectPage(
        items=[
            ProjectSummary(
                project=project,
                progress=ProjectProgress(total=5, completed=2, remaining=3),
                color=color,
            ),
        ],
        next_cursor=None,
    )
    repository.list_projects.assert_awaited_once_with(
        USER_ID,
        include_archived=False,
        limit=DEFAULT_PAGE_SIZE + 1,
        after_archived=None,
        after_name=None,
        after_project_id=None,
    )
    repository.project_progress_for_projects.assert_awaited_once_with(USER_ID, [project.id])
    palette_repository.get_palette_colors_by_ids.assert_awaited_once_with([color.id])
    repository.project_progress.assert_not_awaited()
    palette_repository.get_palette_color.assert_not_awaited()


async def test_list_uses_grouped_progress_and_colors() -> None:
    """A project page loads progress and colors with one batch query each."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    first = make_project(project_id=1, name="Alpha")
    second = make_project(project_id=2, name="Beta", color_id=2)
    first_color = make_color(color_id=1)
    second_color = make_color(color_id=2)
    repository.list_projects.return_value = [(first, "alpha"), (second, "beta")]
    repository.project_progress_for_projects.return_value = {1: (5, 2), 2: (1, 1)}
    palette_repository.get_palette_colors_by_ids.return_value = {
        1: first_color,
        2: second_color,
    }

    service = make_service(repository, palette_repository)

    page = await service.list(
        USER_ID,
        include_archived=False,
        limit=DEFAULT_PAGE_SIZE,
    )

    assert [summary.project for summary in page.items] == [first, second]
    assert page.items[0].progress == ProjectProgress(total=5, completed=2, remaining=3)
    assert page.items[1].color is second_color
    repository.project_progress_for_projects.assert_awaited_once_with(USER_ID, [1, 2])
    palette_repository.get_palette_colors_by_ids.assert_awaited_once_with([1, 2])
    repository.project_progress.assert_not_awaited()
    palette_repository.get_palette_color.assert_not_awaited()


async def test_list_returns_cursor_from_last_kept_project() -> None:
    """An extra repository row signals another page and yields its cursor."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    projects = [
        make_project(project_id=1, name="Alpha"),
        make_project(project_id=2, name="Beta", archived_at=NOW),
        make_project(project_id=3, name="Gamma"),
    ]
    color = make_color()
    repository.list_projects.return_value = [
        (projects[0], "alpha"),
        (projects[1], "beta-db"),
        (projects[2], "gamma"),
    ]
    repository.project_progress_for_projects.return_value = {}
    palette_repository.get_palette_colors_by_ids.return_value = {color.id: color}

    service = make_service(repository, palette_repository)

    page = await service.list(USER_ID, include_archived=True, limit=2)

    assert [summary.project.id for summary in page.items] == [1, 2]
    assert page.next_cursor == ProjectListCursor(
        is_archived=True,
        name_key="beta-db",
        project_id=2,
    )


async def test_list_hides_archived_projects_unless_requested() -> None:
    """Archived projects disappear from active lists but remain available."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    active = make_project(project_id=1, name="Active")
    archived = make_project(project_id=2, name="Archived", archived_at=NOW)
    color = make_color()

    async def list_projects(
        user_id: int,
        *,
        include_archived: bool,
        limit: int,
        after_archived: bool | None,
        after_name: str | None,
        after_project_id: int | None,
    ) -> list[tuple[Project, str]]:
        """Mimic the repository archive filter, name keys, and page bound."""
        assert user_id == USER_ID
        rows = (
            [(active, "active")]
            if not include_archived
            else [
                (active, "active"),
                (archived, "archived"),
            ]
        )
        return rows[:limit]

    repository.list_projects.side_effect = list_projects
    repository.project_progress_for_projects.return_value = {1: (0, 0), 2: (0, 0)}
    palette_repository.get_palette_colors_by_ids.return_value = {color.id: color}

    service = make_service(repository, palette_repository)

    active_result = await service.list(
        USER_ID,
        include_archived=False,
        limit=DEFAULT_PAGE_SIZE,
    )
    all_result = await service.list(
        USER_ID,
        include_archived=True,
        limit=DEFAULT_PAGE_SIZE,
    )

    assert [summary.project for summary in active_result.items] == [active]
    assert [summary.project for summary in all_result.items] == [active, archived]


async def test_list_raises_not_found_for_missing_color() -> None:
    """A page that references a missing palette color fails like a single lookup."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    project = make_project()
    repository.list_projects.return_value = [(project, "work")]
    repository.project_progress_for_projects.return_value = {project.id: (0, 0)}
    palette_repository.get_palette_colors_by_ids.return_value = {}

    service = make_service(repository, palette_repository)

    with pytest.raises(NotFoundError) as error:
        await service.list(USER_ID, include_archived=False, limit=DEFAULT_PAGE_SIZE)

    assert error.value.code is ErrorCode.NOT_FOUND


async def test_get_summary_returns_progress_and_color() -> None:
    """A single project summary uses one project, progress, and color lookup."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    project = make_project()
    color = make_color()
    repository.get_project.return_value = project
    repository.project_progress.return_value = (5, 2)
    palette_repository.get_palette_color.return_value = color

    service = make_service(repository, palette_repository)

    result = await service.get_summary(USER_ID, project.id)

    assert result == ProjectSummary(
        project=project,
        progress=ProjectProgress(total=5, completed=2, remaining=3),
        color=color,
    )
    repository.get_project.assert_awaited_once_with(USER_ID, project.id, for_update=False)
    repository.project_progress.assert_awaited_once_with(USER_ID, project.id)
    palette_repository.get_palette_color.assert_awaited_once_with(color.id)
    repository.list_projects.assert_not_awaited()


async def test_get_returns_project() -> None:
    """An owned project is returned by id."""
    repository = make_repository()
    project = make_project()
    repository.get_project.return_value = project

    service = make_service(repository)

    result = await service.get(USER_ID, project.id)

    assert result is project
    repository.get_project.assert_awaited_once_with(USER_ID, project.id, for_update=False)


async def test_get_cross_user_project_raises_not_found() -> None:
    """Another user's project is indistinguishable from a missing project."""
    repository = make_repository()
    repository.get_project.return_value = None

    service = make_service(repository)

    with pytest.raises(NotFoundError) as error:
        await service.get(USER_ID, 999)

    assert error.value.code is ErrorCode.NOT_FOUND


async def test_create_trims_name_and_uses_default_color() -> None:
    """Project creation strips the name and resolves the default active color."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    color = make_color()
    repository.project_name_exists.return_value = False
    palette_repository.get_default_palette_color.return_value = color
    repository.add_project.side_effect = _return_project

    service = make_service(repository, palette_repository)

    result = await service.create(
        USER_ID,
        name="  Work  ",
        description="Description",
        color_id=None,
        icon="briefcase-business",
    )

    assert result.user_id == USER_ID
    assert result.name == "Work"
    assert result.description == "Description"
    assert result.color_id == color.id
    assert result.icon == "briefcase-business"
    repository.project_name_exists.assert_awaited_once_with(USER_ID, "Work")
    palette_repository.get_default_palette_color.assert_awaited_once_with()
    repository.add_project.assert_awaited_once_with(result)


async def test_create_rejects_empty_name() -> None:
    """A project name that is empty after trimming is rejected."""
    repository = make_repository()

    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.create(USER_ID, name="   ", description=None, color_id=None, icon=None)

    assert error.value.code is ErrorCode.BAD_REQUEST
    repository.add_project.assert_not_awaited()


async def test_create_rejects_duplicate_active_name() -> None:
    """A duplicate active project name raises a conflict."""
    repository = make_repository()
    repository.project_name_exists.return_value = True

    service = make_service(repository)

    with pytest.raises(ConflictError) as error:
        await service.create(USER_ID, name="Work", description=None, color_id=None, icon=None)

    assert str(error.value) == "Project name already exists"
    repository.add_project.assert_not_awaited()


async def test_update_changes_trimmed_name_and_clears_optional_fields() -> None:
    """Project updates trim names and clear optional fields when explicitly None."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    project = make_project()
    project.description = "Old"
    project.icon = "leaf"
    new_color = make_color(color_id=2)
    repository.get_project.return_value = project
    repository.project_name_exists.return_value = False
    palette_repository.get_palette_color.return_value = new_color

    service = make_service(repository, palette_repository)

    result = await service.update(
        USER_ID,
        project.id,
        name="  Renamed  ",
        description=None,
        color_id=new_color.id,
        icon=None,
    )

    assert result is project
    assert project.name == "Renamed"
    assert project.description is None
    assert project.icon is None
    assert project.color_id == new_color.id
    repository.project_name_exists.assert_awaited_once_with(
        USER_ID,
        "Renamed",
        exclude_project_id=project.id,
    )
    palette_repository.get_palette_color.assert_awaited_once_with(new_color.id)
    repository.save_project.assert_awaited_once_with(project)


async def test_update_keeps_omitted_optional_fields() -> None:
    """Omitted description and icon values keep their current project values."""
    repository = make_repository()
    project = make_project()
    project.description = "Keep"
    project.icon = "leaf"
    repository.get_project.return_value = project

    service = make_service(repository)

    await service.update(USER_ID, project.id)

    assert project.description == "Keep"
    assert project.icon == "leaf"
    repository.save_project.assert_awaited_once_with(project)


async def test_update_cannot_move_to_inactive_color() -> None:
    """Moving a project to an inactive palette color is rejected."""
    repository = make_repository()
    palette_repository = make_palette_repository()
    project = make_project(color_id=1)
    repository.get_project.return_value = project
    palette_repository.get_palette_color.return_value = ColorPalette(
        id=9,
        slug="inactive",
        name="Inactive",
        hex="#000000",
        sort_order=9,
        is_active=False,
    )

    service = make_service(repository, palette_repository)

    with pytest.raises(BadRequestError) as error:
        await service.update(USER_ID, project.id, color_id=9)

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert project.color_id == 1
    repository.save_project.assert_not_awaited()


async def test_update_rejects_empty_name() -> None:
    """An update name that is empty after trimming is rejected."""
    repository = make_repository()
    project = make_project()
    repository.get_project.return_value = project

    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, project.id, name="  ")

    repository.save_project.assert_not_awaited()


async def test_update_rejects_duplicate_name() -> None:
    """An update cannot take a name used by another active project."""
    repository = make_repository()
    project = make_project()
    repository.get_project.return_value = project
    repository.project_name_exists.return_value = True

    service = make_service(repository)

    with pytest.raises(ConflictError) as error:
        await service.update(USER_ID, project.id, name="Other")

    assert str(error.value) == "Project name already exists"
    repository.save_project.assert_not_awaited()


async def test_archive_sets_archived_at() -> None:
    """Archiving records the injected current time."""
    repository = make_repository()
    reminder_repository = make_reminder_repository()
    project = make_project()
    repository.get_project.return_value = project

    service = make_service(repository, reminder_repository=reminder_repository)

    result = await service.archive(USER_ID, project.id)

    assert result is project
    assert project.archived_at == NOW
    repository.get_project.assert_awaited_once_with(USER_ID, project.id, for_update=True)
    reminder_repository.pause_project.assert_awaited_once_with(USER_ID, project.id)
    repository.save_project.assert_awaited_once_with(project)


async def test_archive_keeps_existing_archive_timestamp() -> None:
    """Archiving an already archived project does not move its timestamp."""
    older = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    repository = make_repository()
    reminder_repository = make_reminder_repository()
    project = make_project(archived_at=older)
    repository.get_project.return_value = project

    service = make_service(repository, reminder_repository=reminder_repository)

    await service.archive(USER_ID, project.id)

    assert project.archived_at == older
    reminder_repository.pause_project.assert_not_awaited()
    repository.save_project.assert_not_awaited()


async def test_project_archive_delegates_reminder_pause() -> None:
    """Project archive pauses project reminders and persists the entity."""
    repository = FakeTaskManagementRepository()
    reminder_repository = make_reminder_repository()
    project = repository.insert_project(make_project())
    service = ProjectService(
        repository,
        PaletteService(repository),
        reminder_repository,
        clock=fixed_now,
    )

    archived = await service.archive(USER_ID, project.id)

    assert archived.archived_at == fixed_now()
    assert repository.saved_projects == [project.id]
    reminder_repository.pause_project.assert_awaited_once_with(USER_ID, project.id)


async def test_unarchive_clears_archived_at() -> None:
    """Unarchiving a project makes it active again."""
    repository = make_repository()
    reminder_repository = make_reminder_repository()
    project = make_project(archived_at=NOW)
    repository.get_project.return_value = project
    repository.project_name_exists.return_value = False

    service = make_service(repository, reminder_repository=reminder_repository)

    result = await service.unarchive(USER_ID, project.id)

    assert result is project
    assert project.archived_at is None
    repository.project_name_exists.assert_awaited_once_with(
        USER_ID,
        project.name,
        exclude_project_id=project.id,
    )
    reminder_repository.resume_project.assert_awaited_once_with(
        USER_ID,
        project.id,
        NOW,
    )
    repository.save_project.assert_awaited_once_with(project)


async def test_unarchive_rebuilds_every_series_and_task_reminder() -> None:
    """Unarchiving a project mutates reminder slots for every series and task."""
    from datetime import date, time, timedelta

    from backend.task_management.enums import ReminderKind
    from tests.task_management_fakes import (
        make_reminder,
        make_series,
        make_task,
    )

    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=NOW))
    first_series = repository.insert_series(
        make_series(
            project_id=project.id,
            time_of_day=time(9, 0),
            timezone="Asia/Tokyo",
            starts_on=date(2026, 11, 1),
        )
    )
    second_series = repository.insert_series(
        make_series(
            project_id=project.id,
            time_of_day=time(10, 0),
            timezone="Europe/Berlin",
            starts_on=date(2026, 11, 1),
        )
    )
    first_template = repository.insert_reminder(
        make_reminder(
            series_id=first_series.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=NOW,
        )
    )
    second_template = repository.insert_reminder(
        make_reminder(
            series_id=second_series.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(10, 0),
            timezone="UTC",
            next_run_at=NOW,
        )
    )
    first_task = repository.insert_task(
        make_task(
            project_id=project.id,
            due_at=NOW + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    second_task = repository.insert_task(
        make_task(
            project_id=project.id,
            due_at=NOW + timedelta(days=2),
            reminders_enabled=True,
        )
    )
    first_task_reminder = repository.insert_reminder(
        make_reminder(
            task_id=first_task.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=NOW,
        )
    )
    second_task_reminder = repository.insert_reminder(
        make_reminder(
            task_id=second_task.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=15,
            next_run_at=NOW,
        )
    )
    service = ProjectService(
        repository,
        PaletteService(repository),
        ProjectReminderService(repository),
        clock=clock,
    )

    await service.unarchive(USER_ID, project.id)

    assert first_template.next_run_at is None
    assert first_template.timezone == "Asia/Tokyo"
    assert second_template.next_run_at is None
    assert second_template.timezone == "Europe/Berlin"
    assert first_task_reminder.next_run_at == first_task.due_at - timedelta(minutes=30)
    assert second_task_reminder.next_run_at == second_task.due_at - timedelta(minutes=15)
    assert repository.saved_projects == [project.id]
    assert repository.save_reminder_batches == 1


async def test_unarchive_rejects_name_conflict_with_active_project() -> None:
    """Unarchiving a project whose name is active elsewhere raises a conflict."""
    repository = make_repository()
    reminder_repository = make_reminder_repository()
    project = make_project(name="Work", archived_at=NOW)
    repository.get_project.return_value = project
    repository.project_name_exists.return_value = True

    service = make_service(repository, reminder_repository=reminder_repository)

    with pytest.raises(ConflictError) as error:
        await service.unarchive(USER_ID, project.id)

    assert str(error.value) == "Project name already exists"
    assert project.archived_at == NOW
    reminder_repository.resume_project.assert_not_awaited()
    repository.save_project.assert_not_awaited()


async def test_delete_soft_deletes_project_with_batch_and_cancels_deliveries() -> None:
    """Deleting a project uses one batch and delegates cascade and cancellation."""
    repository = make_repository()
    project = make_project()
    repository.get_project.return_value = project

    service = make_service(repository)

    await service.delete(USER_ID, project.id)

    repository.soft_delete_project.assert_awaited_once()
    call = repository.soft_delete_project.await_args
    assert call is not None
    assert call.args[0] is project
    assert isinstance(call.args[1], UUID)
    assert call.args[2] == NOW
    repository.get_project.assert_awaited_once_with(USER_ID, project.id, for_update=True)
    repository.save_project.assert_not_awaited()


async def test_stats_returns_active_archived_and_planned_counts() -> None:
    """Dashboard statistics combine project counts with planned tasks."""
    repository = make_repository()

    async def count_projects(user_id: int, *, archived: bool) -> int:
        """Mimic active and archived project counts."""
        assert user_id == USER_ID
        return 1 if archived else 3

    repository.count_projects.side_effect = count_projects
    repository.planned_tasks_count.return_value = 4

    service = make_service(repository)

    result = await service.stats(USER_ID)

    assert result == {"active": 3, "archived": 1, "planned": 4}
    repository.planned_tasks_count.assert_awaited_once_with(USER_ID)


async def test_delete_cross_user_project_raises_not_found() -> None:
    """Another user's project cannot be deleted."""
    repository = make_repository()
    repository.get_project.return_value = None

    service = make_service(repository)

    with pytest.raises(NotFoundError):
        await service.delete(USER_ID, 999)

    repository.soft_delete_project.assert_not_awaited()


async def test_project_update_persists_mutated_domain_entity() -> None:
    """Project updates call save_project after mutating the domain entity."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(name="Old"))
    service = ProjectService(
        repository,
        PaletteService(repository),
        ProjectReminderService(repository),
        clock=fixed_now,
    )

    updated = await service.update(USER_ID, project.id, name="New")

    assert updated.name == "New"
    assert repository.saved_projects == [project.id]
    assert repository.projects[project.id].name == "New"
