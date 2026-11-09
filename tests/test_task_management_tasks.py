from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from backend.api.pagination import DEFAULT_PAGE_SIZE
from backend.core.errors import BadRequestError, ErrorCode, NotFoundError
from backend.task_management.enums import Priority, ReminderKind, TaskRecurrence
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.reminders import ReminderInput
from backend.task_management.series import SeriesService
from backend.task_management.tasks import TaskFilters, TaskListCursor, TaskService
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
    assert repository.task_label_calls == [(detail.task.id, USER_ID, (label.id,))]


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

    visible = await service.list(
        USER_ID,
        TaskFilters(date=date(2026, 11, 4)),
        limit=DEFAULT_PAGE_SIZE,
    )
    all_projects = await service.list(
        USER_ID,
        TaskFilters(
            date=date(2026, 11, 4),
            include_archived_projects=True,
        ),
        limit=DEFAULT_PAGE_SIZE,
    )

    assert [detail.task.title for detail in visible.items] == ["Today"]
    assert [detail.task.title for detail in all_projects.items] == ["Today", "Hidden"]


async def test_list_returns_page_and_cursor() -> None:
    """Return the requested page size and a cursor built from the last kept task."""
    repository = FakeTaskManagementRepository()
    due_at = fixed_now()
    for task_id in (1, 2, 3):
        repository.insert_task(
            make_task(task_id=task_id, due_at=due_at + timedelta(minutes=task_id))
        )
    service = make_service(repository)

    page = await service.list(USER_ID, TaskFilters(), limit=2)
    last_page = await service.list(
        USER_ID,
        TaskFilters(),
        limit=2,
        after=page.next_cursor,
    )

    assert [detail.task.id for detail in page.items] == [1, 2]
    assert page.next_cursor == TaskListCursor(
        due_at=due_at + timedelta(minutes=2),
        task_id=2,
    )
    assert [detail.task.id for detail in last_page.items] == [3]
    assert last_page.next_cursor is None


async def test_list_uses_batch_related_queries() -> None:
    """Enrich a page through one batch query per related collection."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(project_id=1))
    label = repository.insert_label(make_label(label_id=1))
    due_at = fixed_now()
    for task_id in (1, 2, 3):
        repository.insert_task(
            make_task(
                task_id=task_id,
                project_id=project.id,
                due_at=due_at + timedelta(minutes=task_id),
            )
        )
        repository.task_labels.add((task_id, label.id))
        repository.insert_reminder(make_reminder(task_id=task_id))
    service = make_service(repository)

    page = await service.list(USER_ID, TaskFilters(), limit=DEFAULT_PAGE_SIZE)

    assert [detail.project for detail in page.items] == [project, project, project]
    assert [detail.labels for detail in page.items] == [[label], [label], [label]]
    assert all(len(detail.reminders) == 1 for detail in page.items)
    assert [name for name, _ in repository.batch_calls] == [
        "list_task_labels_for_tasks",
        "list_task_reminders_for_tasks",
        "get_projects_by_ids",
    ]
    assert repository.batch_calls[0][1] == (1, 2, 3)
    assert repository.batch_calls[1][1] == (1, 2, 3)
    assert repository.batch_calls[2][1] == (1,)


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
    assert future.id not in repository.tasks
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


@pytest.mark.parametrize(
    "due_at",
    [
        datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(2101, 1, 1, tzinfo=UTC),
    ],
)
async def test_create_task_rejects_due_at_outside_supported_range(due_at: datetime) -> None:
    """A direct service create rejects due instants outside the shared UTC bounds."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Task",
            description=None,
            priority=Priority.NEITHER,
            due_at=due_at,
            project_id=None,
            label_ids=(),
            reminders_enabled=False,
            reminders=(),
        )

    assert repository.tasks == {}


async def test_update_task_rejects_due_at_outside_supported_range() -> None:
    """A direct service update rejects due instants outside the shared UTC bounds."""
    repository = FakeTaskManagementRepository()
    due_at = fixed_now() + timedelta(hours=1)
    task = repository.insert_task(make_task(task_id=1, due_at=due_at))
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, task.id, due_at=datetime(2101, 1, 1, tzinfo=UTC))

    assert task.due_at == due_at


async def test_list_rejects_date_that_overflows_local_day_bounds() -> None:
    """The last supported calendar date produces a bad request, not a 500."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.list(USER_ID, TaskFilters(date=date.max), limit=DEFAULT_PAGE_SIZE)


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


async def test_enable_regular_on_future_one_off_task_creates_series_without_moving_due() -> None:
    """A future one-off task keeps its due time when converted to a daily series."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Moscow"
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() + timedelta(days=1),
            title="Old",
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        title="New",
        recurrence=TaskRecurrence.REGULAR,
    )

    assert detail.task.title == "New"
    assert detail.task.series_id is not None
    assert detail.recurrence is TaskRecurrence.REGULAR
    assert detail.task.due_at == fixed_now() + timedelta(days=1)
    series = repository.series[detail.task.series_id]
    assert series.frequency == "daily"
    assert series.interval_days == 1


async def test_enable_periodic_on_overdue_one_off_task_shifts_to_next_phase_slot() -> None:
    """An overdue one-off task shifts to the next periodic phase slot on conversion."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Moscow"
    task = repository.insert_task(make_task(due_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC)))
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        recurrence=TaskRecurrence.PERIODIC,
        interval_days=3,
    )

    assert detail.task.due_at == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    assert detail.task.series_id is not None
    series = repository.series[detail.task.series_id]
    assert series.starts_on == date(2026, 11, 7)
    assert series.interval_days == 3
    assert detail.task.due_at in {item.due_at for item in repository.tasks.values()}
    assert sum(1 for item in repository.tasks.values() if item.due_at == detail.task.due_at) == 1


async def test_enable_regular_on_completed_task_keeps_completion_and_series_starts_on_due_date(  # noqa: E501
) -> None:
    """A completed task stays completed and its series phase starts on the due date."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() - timedelta(days=2),
            completed_at=fixed_now() - timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        recurrence=TaskRecurrence.REGULAR,
    )

    assert detail.recurrence is TaskRecurrence.REGULAR
    assert detail.task.completed_at == fixed_now() - timedelta(days=1)
    assert detail.task.due_at == fixed_now() - timedelta(days=2)


async def test_disable_regular_on_future_series_task_detaches_only_current_instance() -> None:
    """Disabling regularity keeps the current instance and removes future siblings."""
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
            due_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        )
    )
    current = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=3,
            series_id=series.id,
            due_at=datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=4,
            series_id=series.id,
            due_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, current.id, recurrence=TaskRecurrence.ONE_OFF)

    assert future.id not in repository.tasks
    assert current.id in repository.tasks
    assert current.series_id is None
    assert current.due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert past.id in repository.tasks
    assert completed.id in repository.tasks
    assert series.deleted_at == fixed_now()
    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_disable_regular_on_overdue_series_task_shifts_current_and_keeps_history() -> None:
    """Disabling regularity shifts only the current overdue task by series phase."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    overdue_history = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
            completed_at=datetime(2026, 11, 3, 10, 0, tzinfo=UTC),
        )
    )
    current = repository.insert_task(
        make_task(
            task_id=3,
            series_id=series.id,
            due_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=4,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, current.id, recurrence=TaskRecurrence.ONE_OFF)

    assert current.due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert current.series_id is None
    assert overdue_history.due_at == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert overdue_history.id in repository.tasks
    assert completed.due_at == datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    assert completed.id in repository.tasks
    assert future.id not in repository.tasks
    assert series.deleted_at == fixed_now()
    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_disable_overdue_series_task_can_reuse_a_removed_future_slot() -> None:
    """Detach removes the future sibling before the overdue task moves there."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    current = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, current.id, recurrence=TaskRecurrence.ONE_OFF)

    assert future.id not in repository.tasks
    assert current.due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert sum(1 for item in repository.tasks.values() if item.due_at == current.due_at) == 1
    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_disable_regular_on_completed_series_task_keeps_due() -> None:
    """Disabling regularity on a completed task leaves its due time untouched."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now() - timedelta(hours=1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=2),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, completed.id, recurrence=TaskRecurrence.ONE_OFF)

    assert completed.due_at == fixed_now() + timedelta(days=1)
    assert completed.completed_at == fixed_now() - timedelta(hours=1)
    assert completed.series_id is None
    assert future.id not in repository.tasks
    assert series.deleted_at == fixed_now()
    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_update_periodic_series_interval_without_recurrence() -> None:
    """An interval-only edit updates and regenerates an existing periodic series."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            frequency="periodic",
            interval_days=3,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    past = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=3,
            series_id=series.id,
            due_at=datetime(2026, 11, 10, 9, 0, tzinfo=UTC),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, future.id, interval_days=5)

    assert series.frequency == "periodic"
    assert series.interval_days == 5
    assert past.deleted_at is None
    assert completed.deleted_at is None
    assert future.deleted_at == fixed_now()
    generated = sorted(
        (
            task
            for task in repository.tasks.values()
            if task.series_id == series.id and task.id not in {past.id, future.id, completed.id}
        ),
        key=lambda task: task.due_at,
    )
    assert generated
    assert generated[0].due_at == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert all((task.due_at.date() - date(2026, 11, 1)).days % 5 == 0 for task in generated)
    assert detail.recurrence is TaskRecurrence.PERIODIC


async def test_interval_only_on_one_off_or_daily_raises_bad_request() -> None:
    """An interval without recurrence requires an existing periodic series."""
    repository = FakeTaskManagementRepository()
    one_off = repository.insert_task(make_task(task_id=1, due_at=fixed_now() + timedelta(days=1)))
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    daily_task = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, one_off.id, interval_days=5)
    with pytest.raises(BadRequestError):
        await service.update(USER_ID, daily_task.id, interval_days=5)

    assert one_off.series_id is None
    assert series.frequency == "daily"
    assert series.interval_days == 1


@pytest.mark.parametrize(
    ("recurrence", "interval_days"),
    [
        (TaskRecurrence.ONE_OFF, 1),
        (TaskRecurrence.ONE_OFF, 3),
        (TaskRecurrence.REGULAR, 3),
    ],
)
async def test_toggle_rejects_incompatible_explicit_interval(
    recurrence: TaskRecurrence,
    interval_days: int,
) -> None:
    """Reject one-off intervals and daily intervals other than one at the service boundary."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(make_task(due_at=fixed_now() + timedelta(days=1)))
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(
            USER_ID,
            task.id,
            recurrence=recurrence,
            interval_days=interval_days,
        )

    assert task.series_id is None
    assert repository.series == {}


async def test_periodic_requires_interval_on_conversion() -> None:
    """Converting a one-off task to periodic requires an explicit interval."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(make_task(due_at=fixed_now() + timedelta(days=1)))
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, task.id, recurrence=TaskRecurrence.PERIODIC)

    assert task.series_id is None
    assert repository.series == {}


async def test_enable_regular_on_task_with_archived_project_raises_bad_request() -> None:
    """Conversion validates the project like series creation."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() + timedelta(days=1),
            project_id=project.id,
        )
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, task.id, recurrence=TaskRecurrence.REGULAR)

    assert task.series_id is None
    assert repository.series == {}


async def test_enable_regular_on_task_with_deleted_project_raises_not_found() -> None:
    """Conversion rejects a project that no longer exists."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(deleted_at=fixed_now()))
    task = repository.insert_task(
        make_task(
            due_at=fixed_now() + timedelta(days=1),
            project_id=project.id,
        )
    )
    service = make_service(repository)

    with pytest.raises(NotFoundError):
        await service.update(USER_ID, task.id, recurrence=TaskRecurrence.REGULAR)

    assert task.series_id is None
    assert repository.series == {}


async def test_update_task_of_soft_deleted_series_treats_it_as_one_off() -> None:
    """Editing a task of a soft-deleted series works like editing a one-off task."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            title="Old",
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, title="New")

    assert detail.task is task
    assert task.title == "New"
    assert task.series_id == series.id
    assert series.deleted_at == fixed_now()


async def test_update_due_at_on_soft_deleted_series_task_clears_stale_series() -> None:
    """A changed due time detaches a soft-deleted-series task before the unique slot check."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    target_due_at = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    sibling = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=target_due_at)
    )
    task = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, due_at=target_due_at)

    assert task.series_id is None
    assert task.due_at == target_due_at
    assert detail.recurrence is TaskRecurrence.ONE_OFF
    assert series.deleted_at == fixed_now()
    assert sibling.series_id == series.id
    assert sibling.due_at == target_due_at
    assert sibling.deleted_at is None


async def test_disable_regular_on_task_of_soft_deleted_series_clears_series_id() -> None:
    """An explicit one-off toggle clears a stale soft-deleted series reference."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, recurrence=TaskRecurrence.ONE_OFF)

    assert task.series_id is None
    assert series.deleted_at == fixed_now()
    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_reenable_regular_on_task_of_soft_deleted_series_creates_new_series() -> None:
    """Re-enabling regularity replaces a stale soft-deleted series reference."""
    repository = FakeTaskManagementRepository()
    old_series = repository.insert_series(
        make_series(
            series_id=1,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    history = repository.insert_task(
        make_task(
            task_id=1,
            series_id=old_series.id,
            due_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=2,
            series_id=old_series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, recurrence=TaskRecurrence.REGULAR)

    assert detail.recurrence is TaskRecurrence.REGULAR
    assert task.series_id is not None
    assert task.series_id != old_series.id
    new_series = repository.series[task.series_id]
    assert new_series.deleted_at is None
    assert new_series.frequency == "daily"
    assert old_series.deleted_at == fixed_now()
    assert history.series_id == old_series.id
    assert history.id in repository.tasks


async def test_toggle_applies_other_fields_before_conversion() -> None:
    """Fields from the same PATCH are applied before the series snapshot is taken."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    label = repository.insert_label(make_label())
    task = repository.insert_task(
        make_task(
            task_id=1,
            title="Old",
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        title="New",
        description="New description",
        priority=Priority.URGENT_IMPORTANT,
        project_id=project.id,
        label_ids=(label.id,),
        recurrence=TaskRecurrence.REGULAR,
    )

    assert detail.task.title == "New"
    assert detail.task.description == "New description"
    assert detail.task.priority == Priority.URGENT_IMPORTANT.value
    assert detail.task.project_id == project.id
    assert detail.labels == [label]
    assert task.series_id is not None
    series = repository.series[task.series_id]
    assert series.title == "New"
    assert series.description == "New description"
    assert series.priority == Priority.URGENT_IMPORTANT.value
    assert series.project_id == project.id
    assert (series.id, label.id) in repository.series_labels


async def test_toggle_to_regular_on_overdue_task_keeps_supplied_before_due_reminder() -> None:
    """A supplied before-due rule survives the overdue shift and series snapshot."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            due_at=datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
            reminders_enabled=True,
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        recurrence=TaskRecurrence.REGULAR,
        reminders=(
            ReminderInput(
                kind=ReminderKind.BEFORE_DUE,
                offset_minutes=30,
            ),
        ),
    )

    assert task.due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert len(detail.reminders) == 1
    assert detail.reminders[0].kind == ReminderKind.BEFORE_DUE.value
    assert detail.reminders[0].offset_minutes == 30
    assert detail.reminders[0].next_run_at == datetime(2026, 11, 5, 8, 30, tzinfo=UTC)
    assert task.series_id is not None
    templates = await repository.list_series_reminders(USER_ID, task.series_id)
    assert len(templates) == 1
    assert templates[0].kind == ReminderKind.BEFORE_DUE.value
    assert templates[0].offset_minutes == 30


async def test_toggle_to_one_off_on_overdue_series_task_keeps_supplied_before_due_reminder() -> (
    None
):
    """A supplied before-due rule survives the detach overdue shift."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(
            series_id=series.id,
            due_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            reminders_enabled=True,
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        recurrence=TaskRecurrence.ONE_OFF,
        reminders=(
            ReminderInput(
                kind=ReminderKind.BEFORE_DUE,
                offset_minutes=30,
            ),
        ),
    )

    assert task.due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert series.deleted_at == fixed_now()
    assert len(detail.reminders) == 1
    assert detail.reminders[0].kind == ReminderKind.BEFORE_DUE.value
    assert detail.reminders[0].offset_minutes == 30
    assert detail.reminders[0].next_run_at == datetime(2026, 11, 5, 8, 30, tzinfo=UTC)


async def test_delete_completed_series_task_deletes_series_and_future_instances() -> None:
    """Deleting a completed instance also removes its series and future instances."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    completed = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    future = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=2))
    )
    past = repository.insert_task(
        make_task(task_id=3, series_id=series.id, due_at=fixed_now() - timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, completed.id)

    assert completed.deleted_at == fixed_now()
    assert series.deleted_at == fixed_now()
    assert future.id not in repository.tasks
    assert past.deleted_at is None


async def test_delete_overdue_series_task_deletes_series_and_future_instances() -> None:
    """Deleting an overdue instance removes the series and all future instances."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    overdue = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() - timedelta(hours=1))
    )
    future = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, overdue.id)

    assert overdue.deleted_at == fixed_now()
    assert series.deleted_at == fixed_now()
    assert future.id not in repository.tasks


async def test_delete_task_of_soft_deleted_series_deletes_only_task() -> None:
    """Deleting a task of a soft-deleted series leaves the historical series intact."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(make_series(series_id=1, deleted_at=fixed_now()))
    task = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, task.id)

    assert task.deleted_at == fixed_now()
    assert series.deleted_at == fixed_now()


@pytest.mark.parametrize(
    ("frequency", "interval_days", "expected"),
    [
        (None, None, TaskRecurrence.ONE_OFF),
        ("daily", 1, TaskRecurrence.REGULAR),
        ("periodic", 3, TaskRecurrence.PERIODIC),
    ],
)
async def test_task_detail_recurrence_uses_series_frequency(
    frequency: str | None,
    interval_days: int,
    expected: TaskRecurrence,
) -> None:
    """Classify one-off, daily, and periodic tasks from their stored series frequency."""
    repository = FakeTaskManagementRepository()
    series_id = None
    if frequency is not None:
        series = repository.insert_series(
            make_series(
                series_id=1,
                frequency=frequency,
                interval_days=interval_days,
                time_of_day=time(9, 0),
                starts_on=date(2026, 11, 1),
            )
        )
        series_id = series.id
    task = repository.insert_task(
        make_task(series_id=series_id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    detail = await service.get(USER_ID, task.id)

    assert detail.recurrence is expected


async def test_task_detail_classifies_unknown_series_frequency_as_one_off() -> None:
    """Treat an unexpected stored frequency defensively as a one-off task."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            frequency="weekly",
            interval_days=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    detail = await service.get(USER_ID, task.id)

    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_task_detail_recurrence_survives_soft_deleted_series() -> None:
    """A task of a soft-deleted series still reports its historical recurrence."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            frequency="periodic",
            interval_days=3,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
            deleted_at=fixed_now(),
        )
    )
    task = repository.insert_task(
        make_task(series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    detail = await service.get(USER_ID, task.id)

    assert detail.recurrence is TaskRecurrence.PERIODIC


async def test_list_tasks_returns_recurrence_for_mixed_page() -> None:
    """A list page classifies one-off, active-series, and deleted-series tasks."""
    repository = FakeTaskManagementRepository()
    daily = repository.insert_series(
        make_series(
            series_id=1,
            frequency="daily",
            interval_days=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    periodic = repository.insert_series(
        make_series(
            series_id=2,
            frequency="periodic",
            interval_days=3,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    deleted = repository.insert_series(
        make_series(
            series_id=3,
            frequency="periodic",
            interval_days=5,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
            deleted_at=fixed_now(),
        )
    )
    one_off = repository.insert_task(
        make_task(task_id=1, due_at=fixed_now() + timedelta(minutes=1))
    )
    daily_task = repository.insert_task(
        make_task(
            task_id=2,
            series_id=daily.id,
            due_at=fixed_now() + timedelta(minutes=2),
        )
    )
    periodic_task = repository.insert_task(
        make_task(
            task_id=3,
            series_id=periodic.id,
            due_at=fixed_now() + timedelta(minutes=3),
        )
    )
    deleted_task = repository.insert_task(
        make_task(
            task_id=4,
            series_id=deleted.id,
            due_at=fixed_now() + timedelta(minutes=4),
        )
    )
    service = make_service(repository)

    page = await service.list(USER_ID, TaskFilters(), limit=DEFAULT_PAGE_SIZE)

    assert [detail.task.id for detail in page.items] == [
        one_off.id,
        daily_task.id,
        periodic_task.id,
        deleted_task.id,
    ]
    assert [detail.recurrence for detail in page.items] == [
        TaskRecurrence.ONE_OFF,
        TaskRecurrence.REGULAR,
        TaskRecurrence.PERIODIC,
        TaskRecurrence.PERIODIC,
    ]


async def test_delete_active_future_series_task_still_deletes_series_and_all_future_instances(  # noqa: E501
) -> None:
    """Deleting an active future instance keeps the existing series-wide rule."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    earlier_future = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    target = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=2),
        )
    )
    service = make_service(repository)

    await service.delete(USER_ID, target.id)

    assert series.deleted_at == fixed_now()
    assert earlier_future.id not in repository.tasks
    assert target.id not in repository.tasks


async def test_lock_update_target_treats_soft_deleted_series_as_one_off() -> None:
    """Updating a task whose series is soft-deleted changes only that task."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
            deleted_at=fixed_now(),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            title="Old",
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, title="New")

    assert detail.task is task
    assert task.title == "New"
    assert task.series_id == series.id
    assert series.deleted_at == fixed_now()


async def test_task_detail_classifies_dangling_series_reference_as_one_off() -> None:
    """A task referencing a missing series is classified as one-off."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(make_task(series_id=999, due_at=fixed_now() + timedelta(days=1)))
    service = make_service(repository)

    detail = await service.get(USER_ID, task.id)

    assert detail.recurrence is TaskRecurrence.ONE_OFF


async def test_list_batches_series_lookup_for_distinct_ids_including_deleted() -> None:
    """A page with series tasks issues one deleted-inclusive batch series query."""
    repository = FakeTaskManagementRepository()
    daily = repository.insert_series(
        make_series(
            series_id=1,
            frequency="daily",
            interval_days=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    periodic = repository.insert_series(
        make_series(
            series_id=2,
            frequency="periodic",
            interval_days=3,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    repository.insert_task(make_task(task_id=1, due_at=fixed_now() + timedelta(minutes=1)))
    repository.insert_task(
        make_task(
            task_id=2,
            series_id=daily.id,
            due_at=fixed_now() + timedelta(minutes=2),
        )
    )
    repository.insert_task(
        make_task(
            task_id=3,
            series_id=daily.id,
            due_at=fixed_now() + timedelta(minutes=3),
        )
    )
    repository.insert_task(
        make_task(
            task_id=4,
            series_id=periodic.id,
            due_at=fixed_now() + timedelta(minutes=4),
        )
    )
    service = make_service(repository)

    page = await service.list(USER_ID, TaskFilters(), limit=DEFAULT_PAGE_SIZE)

    assert len(page.items) == 4
    assert repository.series_batch_calls == [((daily.id, periodic.id), True)]


async def test_list_skips_series_batch_query_when_no_task_has_series() -> None:
    """A page without series tasks issues no batch series query."""
    repository = FakeTaskManagementRepository()
    repository.insert_task(make_task(task_id=1, due_at=fixed_now() + timedelta(minutes=1)))
    repository.insert_task(make_task(task_id=2, due_at=fixed_now() + timedelta(minutes=2)))
    service = make_service(repository)

    page = await service.list(USER_ID, TaskFilters(), limit=DEFAULT_PAGE_SIZE)

    assert len(page.items) == 2
    assert repository.series_batch_calls == []


async def test_list_empty_page_issues_no_batch_queries() -> None:
    """An empty page skips every related-data batch query."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    page = await service.list(USER_ID, TaskFilters(), limit=DEFAULT_PAGE_SIZE)

    assert page.items == []
    assert repository.batch_calls == []
    assert repository.series_batch_calls == []


async def test_today_progress_uses_local_day_bounds_and_completed_state() -> None:
    """Today progress uses the user's local day and completion timestamps."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Berlin"
    repository.insert_task(
        make_task(
            task_id=1,
            due_at=datetime(2026, 11, 3, 23, 0, tzinfo=UTC),
            completed_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        )
    )
    repository.insert_task(make_task(task_id=2, due_at=datetime(2026, 11, 4, 22, 59, tzinfo=UTC)))
    repository.insert_task(make_task(task_id=3, due_at=datetime(2026, 11, 4, 23, 0, tzinfo=UTC)))
    repository.insert_task(
        make_task(
            task_id=4,
            due_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
            deleted_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    result = await service.today_progress(USER_ID)

    assert result == (2, 1)


async def test_today_progress_defaults_to_utc_when_timezone_unset() -> None:
    """An unset user timezone falls back to UTC day bounds."""
    repository = FakeTaskManagementRepository()
    repository.insert_task(
        make_task(
            task_id=1,
            due_at=datetime(2026, 11, 4, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 11, 4, 8, 0, tzinfo=UTC),
        )
    )
    repository.insert_task(make_task(task_id=2, due_at=datetime(2026, 11, 4, 23, 59, tzinfo=UTC)))
    repository.insert_task(make_task(task_id=3, due_at=datetime(2026, 11, 5, 0, 0, tzinfo=UTC)))
    service = make_service(repository)

    assert await service.today_progress(USER_ID) == (2, 1)


async def test_today_progress_rejects_invalid_user_timezone() -> None:
    """An invalid stored timezone is a bad request."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Invalid/Zone"
    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.today_progress(USER_ID)

    assert error.value.code is ErrorCode.BAD_REQUEST


@pytest.mark.parametrize(
    ("timezone_name", "clock", "due_at"),
    [
        (
            "Europe/Berlin",
            datetime(2026, 11, 5, 23, 30, tzinfo=UTC),
            datetime(2026, 11, 5, 23, 30, tzinfo=UTC),
        ),
        (
            "America/New_York",
            datetime(2026, 11, 5, 3, 0, tzinfo=UTC),
            datetime(2026, 11, 4, 23, 0, tzinfo=UTC),
        ),
    ],
)
async def test_today_progress_uses_local_date_near_utc_midnight(
    timezone_name: str,
    clock: datetime,
    due_at: datetime,
) -> None:
    """The user's local date, not the UTC date, selects today's tasks."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = timezone_name
    repository.insert_task(make_task(task_id=1, due_at=due_at))
    service = TaskService(
        repository,
        make_series_service(repository),
        clock=lambda: clock,
    )

    assert await service.today_progress(USER_ID) == (1, 0)
