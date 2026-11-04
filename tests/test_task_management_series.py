from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from backend.core.errors import ErrorCode, NotFoundError
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.series import SeriesService
from tests.task_management_fakes import (
    FakeTaskManagementRepository,
    fixed_now,
    make_label,
    make_project,
    make_reminder,
    make_series,
    make_task,
)

USER_ID = 7


def make_service(repository: FakeTaskManagementRepository) -> SeriesService:
    """Build a real series service over the fake repository."""
    palette = PaletteService(repository)
    return SeriesService(
        repository,
        ProjectService(repository, palette, clock=fixed_now),
        LabelService(repository, palette, clock=fixed_now),
        clock=fixed_now,
    )


async def test_create_series_in_the_past_starts_from_the_next_future_slot() -> None:
    """A past start date never backfills occurrences."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    series = await service.create(
        USER_ID,
        title="Daily",
        description=None,
        priority=Priority.NEITHER,
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=date(2026, 11, 1),
        ends_on=None,
        project_id=None,
        label_ids=(),
        reminders_enabled=False,
        reminders=(),
    )

    tasks = sorted(
        (task for task in repository.tasks.values() if task.series_id == series.id),
        key=lambda task: task.due_at,
    )
    assert tasks
    assert min(task.due_at for task in tasks) == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert all(task.due_at > fixed_now() for task in tasks)
    assert series.generated_through == tasks[-1].due_at


async def test_create_series_with_future_start_beyond_horizon_creates_one_instance() -> None:
    """A single first occurrence is created even beyond the 90-day horizon."""
    repository = FakeTaskManagementRepository()
    starts_on = fixed_now().date() + timedelta(days=200)
    service = make_service(repository)

    series = await service.create(
        USER_ID,
        title="Daily",
        description=None,
        priority=Priority.NEITHER,
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=starts_on,
        ends_on=None,
        project_id=None,
        label_ids=(),
        reminders_enabled=False,
        reminders=(),
    )

    tasks = [task for task in repository.tasks.values() if task.series_id == series.id]
    assert len(tasks) == 1
    assert tasks[0].due_at == datetime.combine(starts_on, time(9, 0), tzinfo=UTC)


async def test_generation_copies_snapshot_fields_and_label_ids() -> None:
    """Generated instances copy the series snapshot and label assignments."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    label = repository.insert_label(make_label())
    service = make_service(repository)

    series = await service.create(
        USER_ID,
        title="Read",
        description="Twenty pages",
        priority=Priority.IMPORTANT_NOT_URGENT,
        time_of_day=time(18, 0),
        timezone="UTC",
        starts_on=fixed_now().date(),
        ends_on=None,
        project_id=project.id,
        label_ids=(label.id,),
        reminders_enabled=True,
        reminders=(),
    )

    tasks = sorted(
        (task for task in repository.tasks.values() if task.series_id == series.id),
        key=lambda task: task.due_at,
    )
    first = tasks[0]
    assert first.title == "Read"
    assert first.description == "Twenty pages"
    assert first.priority == Priority.IMPORTANT_NOT_URGENT.value
    assert first.project_id == project.id
    assert repository.task_labels == {(task.id, label.id) for task in tasks}
    assert [item.id for item in await repository.list_task_labels(USER_ID, first.id)] == [label.id]


async def test_generation_is_idempotent_for_an_existing_slot() -> None:
    """Generation skips a slot already present for the series."""
    repository = FakeTaskManagementRepository()
    starts_on = fixed_now().date() + timedelta(days=200)
    series = repository.insert_series(
        make_series(
            series_id=1,
            title="Daily",
            time_of_day=time(9, 0),
            starts_on=starts_on,
        )
    )
    due_at = datetime.combine(starts_on, time(9, 0), tzinfo=UTC)
    existing = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=due_at,
        )
    )
    service = make_service(repository)

    created = await service.generate(fixed_now())

    assert created == 0
    assert list(repository.tasks) == [existing.id]
    assert series.generated_through == fixed_now()


async def test_update_series_only_touches_future_incomplete_instances() -> None:
    """Series updates skip past and completed instances and refresh future labels."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    old_label = repository.insert_label(make_label(label_id=1, name="Old"))
    new_label = repository.insert_label(make_label(label_id=2, name="New"))
    series = repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            title="Old",
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    repository.series_labels.add((series.id, old_label.id))
    past = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            project_id=project.id,
            title="Old",
            due_at=fixed_now() - timedelta(days=1),
        )
    )
    repository.task_labels.add((past.id, old_label.id))
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            project_id=project.id,
            title="Old",
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    repository.task_labels.add((future.id, old_label.id))
    completed = repository.insert_task(
        make_task(
            task_id=3,
            series_id=series.id,
            project_id=project.id,
            title="Old",
            due_at=fixed_now() + timedelta(days=2),
            completed_at=fixed_now(),
        )
    )
    repository.task_labels.add((completed.id, old_label.id))
    service = make_service(repository)

    updated = await service.update(
        USER_ID,
        series.id,
        title="New",
        description="Updated",
        priority=Priority.URGENT_NOT_IMPORTANT,
        label_ids=(new_label.id,),
    )

    assert updated.title == "New"
    assert updated.description == "Updated"
    assert updated.priority == Priority.URGENT_NOT_IMPORTANT.value
    assert future.title == "New"
    assert future.description == "Updated"
    assert future.priority == Priority.URGENT_NOT_IMPORTANT.value
    assert repository.task_labels == {
        (past.id, old_label.id),
        (future.id, new_label.id),
        (completed.id, old_label.id),
    }
    assert past.title == "Old"
    assert completed.title == "Old"


async def test_delete_series_soft_deletes_future_incomplete_and_keeps_past() -> None:
    """Deleting a series removes future incomplete instances but keeps history."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    past = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=3,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=2),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    await service.delete(USER_ID, series.id)

    assert series.deleted_at == fixed_now()
    assert future.deleted_at == fixed_now()
    assert past.deleted_at is None
    assert completed.deleted_at is None


async def test_get_cross_user_series_raises_not_found() -> None:
    """Another user's series is indistinguishable from a missing one."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(NotFoundError) as error:
        await service.get(USER_ID, 999)

    assert error.value.code is ErrorCode.NOT_FOUND


async def test_timezone_edit_retargets_template_and_instance_reminders() -> None:
    """Changing only the series timezone retargets reminders to the new zone."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    series = repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=date(2026, 11, 1),
        )
    )
    template = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            series_id=series.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=None,
        )
    )
    old_future = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            project_id=project.id,
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=2,
            task_id=old_future.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    updated = await service.update(USER_ID, series.id, timezone="Asia/Tokyo")

    assert updated.timezone == "Asia/Tokyo"
    assert template.timezone == "Asia/Tokyo"
    assert old_future.deleted_at == fixed_now()
    new_tasks = [
        task
        for task in repository.tasks.values()
        if task.series_id == series.id and task.deleted_at is None
    ]
    assert new_tasks
    for task in new_tasks:
        for reminder in await repository.list_task_reminders(USER_ID, task.id):
            if reminder.kind == ReminderKind.SCHEDULED.value:
                assert reminder.timezone == "Asia/Tokyo"


async def test_edit_driven_regeneration_refuses_archived_project() -> None:
    """A schedule edit does not regenerate instances for an archived project."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    series = repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=date(2026, 11, 1),
        )
    )
    service = make_service(repository)

    updated = await service.update(USER_ID, series.id, time_of_day=time(10, 0))

    assert updated.time_of_day == time(10, 0)
    assert not [
        task
        for task in repository.tasks.values()
        if task.series_id == series.id and task.deleted_at is None
    ]


async def test_generate_skips_series_locked_by_another_worker() -> None:
    """A series locked by another worker is skipped until the next run."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=fixed_now().date(),
        )
    )
    repository.blocked_series_ids.add(series.id)
    service = make_service(repository)

    created = await service.generate(fixed_now())

    assert created == 0
    assert repository.claimed_series_ids == [series.id]
    assert repository.tasks == {}


async def test_generate_skips_archived_projects() -> None:
    """The periodic generation job never materializes archived project series."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=fixed_now().date(),
        )
    )
    service = make_service(repository)

    created = await service.generate(fixed_now())

    assert created == 0
    assert repository.tasks == {}
