from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from backend.core.errors import BadRequestError, ErrorCode, NotFoundError
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.reminders import ReminderInput
from backend.task_management.series import SeriesService
from backend.task_management.tasks import TaskFilters, TaskService
from tests.task_management_fakes import (
    FakeTaskManagementRepository,
    fixed_now,
    make_delivery,
    make_label,
    make_project,
    make_reminder,
    make_series,
    make_task,
)

USER_ID = 7


def make_series_service(repository: FakeTaskManagementRepository) -> SeriesService:
    """Build a real series service over the fake repository."""
    palette = PaletteService(repository)
    return SeriesService(
        repository,
        ProjectService(repository, palette, clock=fixed_now),
        LabelService(repository, palette, clock=fixed_now),
        clock=fixed_now,
    )


def make_service(repository: FakeTaskManagementRepository) -> TaskService:
    """Build a task service over the fake repository."""
    return TaskService(repository, make_series_service(repository), clock=fixed_now)


async def test_create_one_off_task_inserts_labels_and_before_due_reminder() -> None:
    """Creating a task stores the snapshot, labels, and due-relative reminder."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    label = repository.insert_label(make_label())
    due_at = fixed_now() + timedelta(hours=2)
    service = make_service(repository)

    detail = await service.create(
        USER_ID,
        title="  Buy milk  ",
        description="From the corner shop",
        priority=Priority.IMPORTANT_NOT_URGENT,
        due_at=due_at,
        project_id=project.id,
        label_ids=(label.id,),
        reminders_enabled=True,
        reminders=(
            ReminderInput(
                kind=ReminderKind.BEFORE_DUE,
                offset_minutes=30,
            ),
        ),
    )

    assert detail.task.title == "Buy milk"
    assert detail.task.series_id is None
    assert detail.task.project_id == project.id
    assert detail.project is project
    assert detail.labels == [label]
    assert len(detail.reminders) == 1
    reminder = detail.reminders[0]
    assert reminder.kind == ReminderKind.BEFORE_DUE.value
    assert reminder.offset_minutes == 30
    assert reminder.next_run_at == due_at - timedelta(minutes=30)
    assert repository.tasks[detail.task.id] is detail.task


async def test_create_skips_before_due_reminder_in_the_past() -> None:
    """A before-due moment that has already passed creates no reminder row."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    detail = await service.create(
        USER_ID,
        title="Task",
        description=None,
        priority=Priority.NEITHER,
        due_at=fixed_now() + timedelta(minutes=10),
        project_id=None,
        label_ids=(),
        reminders_enabled=True,
        reminders=(
            ReminderInput(
                kind=ReminderKind.BEFORE_DUE,
                offset_minutes=30,
            ),
        ),
    )

    assert detail.reminders == []
    assert repository.reminders == {}


async def test_create_scheduled_reminder_starts_from_due_date_and_skips_past_slots() -> None:
    """A scheduled reminder takes the first future wall-clock slot."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    detail = await service.create(
        USER_ID,
        title="Task",
        description=None,
        priority=Priority.NEITHER,
        due_at=fixed_now() - timedelta(days=1),
        project_id=None,
        label_ids=(),
        reminders_enabled=True,
        reminders=(
            ReminderInput(
                kind=ReminderKind.SCHEDULED,
                interval_days=1,
                time_of_day=time(9, 0),
            ),
        ),
    )

    reminder = detail.reminders[0]
    assert reminder.kind == ReminderKind.SCHEDULED.value
    assert reminder.interval_days == 1
    assert reminder.time_of_day == time(9, 0)
    assert reminder.timezone == "UTC"
    assert reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


async def test_create_rejects_invalid_reminder_configuration() -> None:
    """A scheduled reminder without an interval is rejected before writing."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.create(
            USER_ID,
            title="Task",
            description=None,
            priority=Priority.NEITHER,
            due_at=fixed_now() + timedelta(hours=1),
            project_id=None,
            label_ids=(),
            reminders_enabled=True,
            reminders=(
                ReminderInput(
                    kind=ReminderKind.SCHEDULED,
                    time_of_day=time(9, 0),
                ),
            ),
        )

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert repository.tasks == {}


async def test_get_cross_user_task_raises_not_found() -> None:
    """Another user's task is indistinguishable from a missing task."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(NotFoundError) as error:
        await service.get(USER_ID, 999)

    assert error.value.code is ErrorCode.NOT_FOUND


async def test_list_applies_filters_and_archived_project_visibility() -> None:
    """Task listing forwards the local-day bounds and effective filters."""
    repository = FakeTaskManagementRepository()
    active = repository.insert_project(make_project(project_id=1))
    archived = repository.insert_project(
        make_project(project_id=2, name="Old", archived_at=fixed_now())
    )
    repository.insert_task(
        make_task(
            task_id=1,
            project_id=active.id,
            title="Today",
            priority=Priority.URGENT_IMPORTANT.value,
            due_at=fixed_now() + timedelta(hours=1),
        )
    )
    repository.insert_task(
        make_task(
            task_id=2,
            project_id=archived.id,
            title="Hidden",
            due_at=fixed_now() + timedelta(hours=2),
        )
    )
    service = make_service(repository)

    visible = await service.list(USER_ID, TaskFilters(date=date(2026, 11, 4)))
    all_projects = await service.list(
        USER_ID,
        TaskFilters(
            date=date(2026, 11, 4),
            include_archived_projects=True,
        ),
    )

    assert [detail.task.title for detail in visible] == ["Today"]
    assert [detail.task.title for detail in all_projects] == ["Today", "Hidden"]


async def test_update_series_task_applies_to_future_incomplete_instances_only() -> None:
    """Editing through a series instance updates future incomplete siblings."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    series = repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            title="Old",
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    past = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            project_id=project.id,
            title="Old",
            due_at=fixed_now() - timedelta(days=1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            project_id=project.id,
            title="Old",
            due_at=fixed_now() + timedelta(days=1),
        )
    )
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
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        future.id,
        title="New",
        priority=Priority.URGENT_IMPORTANT,
    )

    assert detail.task.title == "New"
    assert future.title == "New"
    assert future.priority == Priority.URGENT_IMPORTANT.value
    assert past.title == "Old"
    assert completed.title == "Old"
    assert series.title == "New"


async def test_complete_sets_timestamp_and_cancels_deliveries() -> None:
    """Completing a task cancels pending deliveries and clears reminder slots."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            title="Task",
            due_at=fixed_now() + timedelta(hours=1),
            reminders_enabled=True,
        )
    )
    reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            next_run_at=fixed_now() + timedelta(minutes=30),
        )
    )
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=fixed_now() + timedelta(minutes=30),
        )
    )
    service = make_service(repository)

    detail = await service.complete(USER_ID, task.id)

    assert detail.task.completed_at == fixed_now()
    assert reminder.next_run_at is None
    assert delivery.status == "cancelled"
    assert delivery.locked_at is None


async def test_uncomplete_clears_timestamp_and_rebuilds_reminders_from_now() -> None:
    """Uncompleting a task recomputes future reminder slots without backfill."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            title="Task",
            due_at=fixed_now() + timedelta(hours=2),
            completed_at=fixed_now() - timedelta(hours=1),
            reminders_enabled=True,
        )
    )
    reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            offset_minutes=30,
            next_run_at=None,
        )
    )
    service = make_service(repository)

    detail = await service.uncomplete(USER_ID, task.id)

    assert detail.task.completed_at is None
    assert reminder.next_run_at == task.due_at - timedelta(minutes=30)


async def test_delete_one_off_task_soft_deletes_and_cancels_deliveries() -> None:
    """Deleting a one-off task hides it and cancels its active deliveries."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            title="Task",
            due_at=fixed_now() + timedelta(hours=1),
            reminders_enabled=True,
        )
    )
    reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            next_run_at=fixed_now() + timedelta(minutes=30),
        )
    )
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=fixed_now() + timedelta(minutes=30),
        )
    )
    service = make_service(repository)

    await service.delete(USER_ID, task.id)

    assert task.deleted_at == fixed_now()
    assert task.deletion_batch_id is not None
    assert reminder.next_run_at is None
    assert delivery.status == "cancelled"


async def test_delete_series_task_deletes_the_series_and_future_instances() -> None:
    """Deleting a series instance delegates to the series delete rule."""
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
    service = make_service(repository)

    await service.delete(USER_ID, future.id)

    assert series.deleted_at == fixed_now()
    assert future.deleted_at == fixed_now()
    assert past.deleted_at is None


async def test_archive_project_pauses_task_reminders_and_deliveries() -> None:
    """Archiving a project clears slots and cancels active deliveries."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    task = repository.insert_task(
        make_task(
            task_id=1,
            project_id=project.id,
            due_at=fixed_now() + timedelta(hours=1),
            reminders_enabled=True,
        )
    )
    reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            next_run_at=fixed_now() + timedelta(minutes=30),
        )
    )
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=fixed_now() + timedelta(minutes=30),
        )
    )
    service = ProjectService(
        repository,
        PaletteService(repository),
        clock=fixed_now,
    )

    await service.archive(USER_ID, project.id)

    assert project.archived_at == fixed_now()
    assert reminder.next_run_at is None
    assert delivery.status == "cancelled"


async def test_unarchive_resumes_reminders_without_catching_up() -> None:
    """Unarchiving recomputes future slots and retargets series templates."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    series = repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            time_of_day=time(9, 0),
            timezone="Asia/Tokyo",
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
    future = repository.insert_task(
        make_task(
            task_id=1,
            project_id=project.id,
            due_at=fixed_now() + timedelta(hours=2),
            reminders_enabled=True,
        )
    )
    before_due = repository.insert_reminder(
        make_reminder(
            reminder_id=2,
            task_id=future.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=None,
        )
    )
    scheduled = repository.insert_reminder(
        make_reminder(
            reminder_id=3,
            task_id=future.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=None,
        )
    )
    soon = repository.insert_task(
        make_task(
            task_id=2,
            project_id=project.id,
            due_at=fixed_now() + timedelta(minutes=10),
            reminders_enabled=True,
        )
    )
    past_before_due = repository.insert_reminder(
        make_reminder(
            reminder_id=4,
            task_id=soon.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=None,
        )
    )
    service = ProjectService(
        repository,
        PaletteService(repository),
        clock=fixed_now,
    )

    await service.unarchive(USER_ID, project.id)

    assert project.archived_at is None
    assert template.timezone == "Asia/Tokyo"
    assert template.next_run_at is None
    assert before_due.next_run_at == future.due_at - timedelta(minutes=30)
    assert scheduled.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert past_before_due.next_run_at is None


async def test_re_enabling_reminders_rebuilds_slots_from_now() -> None:
    """Re-enabling the master toggle rebuilds reminder slots from now."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            due_at=fixed_now() + timedelta(hours=2),
            reminders_enabled=False,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=None,
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, reminders_enabled=True)

    assert detail.task.reminders_enabled is True
    assert len(detail.reminders) == 1
    assert detail.reminders[0].next_run_at == task.due_at - timedelta(minutes=30)

    await service.update(USER_ID, task.id, reminders_enabled=False)

    assert all(reminder.next_run_at is None for reminder in repository.reminders.values())


async def test_unarchive_resumes_scheduled_reminder_for_overdue_task() -> None:
    """Unarchiving rebuilds scheduled slots for overdue incomplete tasks."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    overdue = repository.insert_task(
        make_task(
            task_id=1,
            project_id=project.id,
            due_at=fixed_now() - timedelta(days=1),
            reminders_enabled=True,
        )
    )
    reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=overdue.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=None,
        )
    )
    service = ProjectService(
        repository,
        PaletteService(repository),
        clock=fixed_now,
    )

    await service.unarchive(USER_ID, project.id)

    assert reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


async def test_disabling_reminders_persists_supplied_rules_before_clearing_slots() -> None:
    """Disabling with supplied rules stores them, then clears their slots."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            due_at=fixed_now() + timedelta(hours=2),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=fixed_now() + timedelta(hours=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        reminders_enabled=False,
        reminders=(
            ReminderInput(
                kind=ReminderKind.BEFORE_DUE,
                offset_minutes=15,
            ),
        ),
    )

    assert detail.task.reminders_enabled is False
    assert len(detail.reminders) == 1
    assert detail.reminders[0].offset_minutes == 15
    assert detail.reminders[0].next_run_at is None

    reenabled = await service.update(USER_ID, task.id, reminders_enabled=True)

    assert len(reenabled.reminders) == 1
    assert reenabled.reminders[0].offset_minutes == 15
    assert reenabled.reminders[0].next_run_at == task.due_at - timedelta(minutes=15)


async def test_create_task_rejects_archived_project() -> None:
    """A one-off task cannot be created inside an archived project."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(project_id=1, archived_at=fixed_now()))
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Task",
            description=None,
            priority=Priority.NEITHER,
            due_at=fixed_now() + timedelta(hours=1),
            project_id=project.id,
            label_ids=(),
            reminders_enabled=False,
            reminders=(),
        )

    assert repository.tasks == {}


async def test_update_task_rejects_archived_project() -> None:
    """A task cannot be moved into an archived project."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(make_task(task_id=1, due_at=fixed_now() + timedelta(hours=1)))
    project = repository.insert_project(make_project(project_id=1, archived_at=fixed_now()))
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, task.id, project_id=project.id)

    assert task.project_id is None


async def test_list_rejects_date_that_overflows_local_day_bounds() -> None:
    """The last supported calendar date produces a bad request, not a 500."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.list(USER_ID, TaskFilters(date=date.max))


async def test_create_task_rejects_aware_reminder_time() -> None:
    """A direct service call rejects an aware reminder wall-clock time."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Task",
            description=None,
            priority=Priority.NEITHER,
            due_at=fixed_now() + timedelta(hours=1),
            project_id=None,
            label_ids=(),
            reminders_enabled=True,
            reminders=(
                ReminderInput(
                    kind=ReminderKind.SCHEDULED,
                    interval_days=1,
                    time_of_day=time(9, 0, tzinfo=UTC),
                ),
            ),
        )


async def test_create_task_rejects_before_due_overflow() -> None:
    """A before-due offset that underflows the datetime range is a bad request."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Task",
            description=None,
            priority=Priority.NEITHER,
            due_at=datetime.min.replace(tzinfo=UTC),
            project_id=None,
            label_ids=(),
            reminders_enabled=True,
            reminders=(
                ReminderInput(
                    kind=ReminderKind.BEFORE_DUE,
                    offset_minutes=1,
                ),
            ),
        )
