from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

import pytest

from backend.core.errors import BadRequestError, ErrorCode, NotFoundError
from backend.models.task import Task
from backend.task_management.enums import Priority, ReminderKind, SeriesFrequency
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.series import SeriesService
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


def make_service(repository: FakeTaskManagementRepository) -> SeriesService:
    """Build a real series service over the fake repository."""
    palette = PaletteService(repository)
    return SeriesService(
        repository,
        ProjectService(repository, palette, clock=fixed_now),
        LabelService(repository, palette, clock=fixed_now),
        clock=fixed_now,
    )


class FlushOrderingRepository(FakeTaskManagementRepository):
    """Fake repository that records whether the linked task was flushed before dedupe."""

    def __init__(self) -> None:
        """Initialize the watched task and ordering flags."""
        super().__init__()
        self.watched_task: Task | None = None
        self.task_flushed_before_due_lookup = False
        self._task_flushed = False

    async def flush(self) -> None:
        """Record a flush that includes the watched task's series link."""
        await super().flush()
        if self.watched_task is not None and self.watched_task.series_id is not None:
            self._task_flushed = True

    async def list_series_due_times(
        self,
        series_id: int,
        user_id: int,
        *,
        now: datetime,
    ) -> Sequence[datetime]:
        """Record whether the task link was flushed before the dedupe query."""
        self.task_flushed_before_due_lookup = self._task_flushed
        return await super().list_series_due_times(series_id, user_id, now=now)


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


async def test_series_label_validation_uses_one_batch_query() -> None:
    """Validate a deduplicated label selection with one repository call."""
    repository = FakeTaskManagementRepository()
    repository.insert_label(make_label(label_id=1))
    repository.insert_label(make_label(label_id=2))
    service = make_service(repository)

    series = await service.create(
        USER_ID,
        title="Daily",
        description=None,
        priority=Priority.NEITHER,
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=fixed_now().date(),
        ends_on=None,
        project_id=None,
        label_ids=(1, 1, 2),
        reminders_enabled=False,
        reminders=(),
    )

    assert repository.label_batch_calls == [((1, 2),)]
    assert repository.series_labels == {(series.id, 1), (series.id, 2)}


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


async def test_series_generation_selection_excludes_ended_series() -> None:
    """Ended series are not candidates for periodic generation."""
    repository = FakeTaskManagementRepository()
    repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() - timedelta(days=10),
            ends_on=fixed_now().date() - timedelta(days=2),
        )
    )

    candidates = await repository.list_series_for_generation(fixed_now())

    assert candidates == []


async def test_series_generation_selection_includes_series_ending_yesterday() -> None:
    """The end-date lower bound includes the previous UTC day."""
    repository = FakeTaskManagementRepository()
    yesterday = fixed_now().date() - timedelta(days=1)
    ending_yesterday = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=yesterday - timedelta(days=10),
            ends_on=yesterday,
        )
    )

    candidates = await repository.list_series_for_generation(fixed_now())

    assert [series.id for series in candidates] == [ending_yesterday.id]


async def test_series_generation_selection_excludes_caught_up_series() -> None:
    """A marker at 89 days is excluded while 88 days remains eligible."""
    repository = FakeTaskManagementRepository()
    repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() - timedelta(days=10),
            generated_through=fixed_now() + timedelta(days=89),
        )
    )
    eligible = repository.insert_series(
        make_series(
            series_id=2,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() - timedelta(days=10),
            generated_through=fixed_now() + timedelta(days=88),
        )
    )

    candidates = await repository.list_series_for_generation(fixed_now())

    assert [series.id for series in candidates] == [eligible.id]


async def test_series_generation_selection_includes_future_series_within_horizon() -> None:
    """A future start without a marker still receives its first occurrence."""
    repository = FakeTaskManagementRepository()
    future = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() + timedelta(days=10),
        )
    )

    candidates = await repository.list_series_for_generation(fixed_now())

    assert [series.id for series in candidates] == [future.id]


async def test_series_generation_selection_skips_far_future_series_after_first_generation() -> None:
    """A far-future marker from the first occurrence excludes the series."""
    repository = FakeTaskManagementRepository()
    starts_on = fixed_now().date() + timedelta(days=200)
    repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=starts_on,
            generated_through=datetime.combine(starts_on, time(9, 0), tzinfo=UTC),
        )
    )

    candidates = await repository.list_series_for_generation(fixed_now())

    assert candidates == []


async def test_list_series_due_times_filters_future_slots() -> None:
    """Due-time lookup ignores slots that have already passed."""
    repository = FakeTaskManagementRepository()
    past = repository.insert_task(
        make_task(
            task_id=1,
            series_id=3,
            due_at=fixed_now() - timedelta(days=1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=3,
            due_at=fixed_now() + timedelta(days=1),
        )
    )

    due_times = await repository.list_series_due_times(3, USER_ID, now=fixed_now())

    assert past.due_at not in due_times
    assert due_times == [future.due_at]


async def test_generate_skips_caught_up_series_and_generates_future_series() -> None:
    """The run ignores a caught-up marker and materializes a future start."""
    repository = FakeTaskManagementRepository()
    caught_up = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() - timedelta(days=10),
            generated_through=fixed_now() + timedelta(days=89),
        )
    )
    future = repository.insert_series(
        make_series(
            series_id=2,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() + timedelta(days=10),
        )
    )
    service = make_service(repository)

    created = await service.generate(fixed_now())

    generated = [task for task in repository.tasks.values() if task.series_id == future.id]
    assert created == len(generated) > 0
    assert repository.claimed_series_ids == [future.id]
    assert min(task.due_at for task in generated) == datetime.combine(
        fixed_now().date() + timedelta(days=10),
        time(9, 0),
        tzinfo=UTC,
    )
    assert all(task.series_id != caught_up.id for task in repository.tasks.values())


async def test_generate_series_uses_bulk_add_methods() -> None:
    """One generation pass batches tasks, label rows, and reminders."""
    repository = FakeTaskManagementRepository()
    label = repository.insert_label(make_label())
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() - timedelta(days=1),
        )
    )
    repository.series_labels.add((series.id, label.id))
    repository.insert_reminder(
        make_reminder(
            series_id=series.id,
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
        )
    )
    service = make_service(repository)

    created = await service.generate(fixed_now())

    assert created > 1
    assert repository.bulk_calls.count("add_tasks") == 1
    assert repository.bulk_calls.count("add_task_label_rows") == 1
    assert repository.bulk_calls.count("add_reminders") == 1
    assert repository.task_labels == {
        (task.id, label.id) for task in repository.tasks.values() if task.series_id == series.id
    }


async def test_generate_series_does_not_claim_ended_series() -> None:
    """The selection query keeps ended series out of the generation claim loop."""
    repository = FakeTaskManagementRepository()
    repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date() - timedelta(days=10),
            ends_on=fixed_now().date() - timedelta(days=2),
        )
    )
    service = make_service(repository)

    created = await service.generate(fixed_now())

    assert created == 0
    assert repository.claimed_series_ids == []
    assert "add_tasks" not in repository.bulk_calls
    assert repository.tasks == {}


async def test_generate_series_preserves_generated_through() -> None:
    """A generation pass without new slots never rewinds the marker."""
    repository = FakeTaskManagementRepository()
    marker = fixed_now()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date(),
            ends_on=fixed_now().date(),
            generated_through=marker,
        )
    )
    service = make_service(repository)

    created = await service.generate(fixed_now() + timedelta(minutes=15))

    assert created == 0
    assert series.generated_through == marker


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


async def test_get_detail_returns_labels_and_template_reminders() -> None:
    """Resolve a series together with its labels and template reminders."""
    repository = FakeTaskManagementRepository()
    label = repository.insert_label(make_label())
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 4),
        )
    )
    repository.series_labels.add((series.id, label.id))
    reminder = repository.insert_reminder(
        make_reminder(
            series_id=series.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
        )
    )
    service = make_service(repository)

    detail = await service.get_detail(USER_ID, series.id)

    assert detail.series is series
    assert [item.id for item in detail.labels] == [label.id]
    assert [item.id for item in detail.reminders] == [reminder.id]


async def test_list_details_returns_every_owned_series() -> None:
    """Return details for all non-deleted series owned by the user."""
    repository = FakeTaskManagementRepository()
    first = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 4))
    )
    repository.insert_series(
        make_series(series_id=2, time_of_day=time(10, 0), starts_on=date(2026, 11, 4))
    )
    service = make_service(repository)

    details = await service.list_details(USER_ID)

    assert [detail.series.id for detail in details] == [first.id, 2]


async def test_resumed_generation_does_not_overshoot_or_rewind_marker() -> None:
    """A run soon after creation creates nothing and keeps ``generated_through``."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)
    series = await service.create(
        USER_ID,
        title="Daily",
        description=None,
        priority=Priority.NEITHER,
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=fixed_now().date(),
        ends_on=None,
        project_id=None,
        label_ids=(),
        reminders_enabled=False,
        reminders=(),
    )
    generated_through = series.generated_through
    task_count = len(repository.tasks)

    created = await service.generate(fixed_now() + timedelta(minutes=15))

    assert created == 0
    assert series.generated_through == generated_through
    assert len(repository.tasks) == task_count


async def test_delete_series_cancels_overdue_instance_reminders_and_keeps_history() -> None:
    """Deleting a series stops overdue reminders but preserves incomplete history."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    overdue = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
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
            next_run_at=fixed_now() - timedelta(minutes=5),
        )
    )
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=fixed_now() - timedelta(minutes=5),
        )
    )
    service = make_service(repository)

    await service.delete(USER_ID, series.id)

    assert overdue.deleted_at is None
    assert reminder.next_run_at is None
    assert delivery.status == "cancelled"
    assert delivery.locked_at is None


async def test_generation_continues_after_one_series_fails_unexpectedly() -> None:
    """A middle-series rollback does not block the series after it."""
    repository = FakeTaskManagementRepository()
    first = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date(),
        )
    )
    bad = repository.insert_series(
        make_series(
            series_id=2,
            time_of_day=time(10, 0),
            starts_on=fixed_now().date(),
        )
    )
    third = repository.insert_series(
        make_series(
            series_id=3,
            time_of_day=time(11, 0),
            starts_on=fixed_now().date(),
        )
    )
    repository.series_errors[bad.id] = RuntimeError("boom")
    service = make_service(repository)

    created = await service.generate(fixed_now())

    assert created > 0
    assert repository.rollbacks == 1
    assert repository.commits >= 1
    assert {task.series_id for task in repository.tasks.values()} == {
        first.id,
        third.id,
    }


async def test_create_series_rejects_aware_time_of_day_at_service_boundary() -> None:
    """A direct service call rejects an aware wall-clock time."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Daily",
            description=None,
            priority=Priority.NEITHER,
            time_of_day=time(9, 0, tzinfo=UTC),
            timezone="UTC",
            starts_on=fixed_now().date(),
            ends_on=None,
            project_id=None,
            label_ids=(),
            reminders_enabled=False,
            reminders=(),
        )

    assert repository.series == {}


async def test_update_series_rejects_aware_time_of_day_at_service_boundary() -> None:
    """A direct update rejects an aware wall-clock time."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=fixed_now().date())
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, series.id, time_of_day=time(9, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    ("starts_on", "ends_on"),
    [
        (date(2024, 12, 31), None),
        (date(2101, 1, 1), None),
        (date(2026, 11, 4), date(2101, 1, 1)),
    ],
)
async def test_create_series_rejects_dates_outside_supported_range(
    starts_on: date,
    ends_on: date | None,
) -> None:
    """A direct service create rejects calendar dates outside the shared bounds."""
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Daily",
            description=None,
            priority=Priority.NEITHER,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=starts_on,
            ends_on=ends_on,
            project_id=None,
            label_ids=(),
            reminders_enabled=False,
            reminders=(),
        )

    assert repository.series == {}


@pytest.mark.parametrize(
    ("starts_on", "ends_on"),
    [
        (date(2024, 12, 31), None),
        (None, date(2101, 1, 1)),
    ],
)
async def test_update_series_rejects_dates_outside_supported_range(
    starts_on: date | None,
    ends_on: date | None,
) -> None:
    """A direct service update rejects calendar dates outside the shared bounds."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 4),
        )
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, series.id, starts_on=starts_on, ends_on=ends_on)

    assert series.starts_on == date(2026, 11, 4)
    assert series.ends_on is None


async def test_create_periodic_series_stores_interval_and_generates_phase_slots() -> None:
    repository = FakeTaskManagementRepository()
    service = make_service(repository)

    series = await service.create(
        USER_ID,
        title="Periodic",
        description=None,
        priority=Priority.NEITHER,
        frequency=SeriesFrequency.PERIODIC,
        interval_days=3,
        time_of_day=time(9, 0),
        timezone="Europe/Moscow",
        starts_on=date(2026, 11, 1),
        ends_on=None,
        project_id=None,
        label_ids=(),
        reminders_enabled=False,
        reminders=(),
    )

    assert series.frequency == "periodic"
    assert series.interval_days == 3
    due_times = sorted(task.due_at for task in repository.tasks.values())
    assert due_times[0] == datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    assert due_times[1] == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("frequency", "interval_days"),
    [
        (SeriesFrequency.DAILY, 3),
        (SeriesFrequency.PERIODIC, None),
        (SeriesFrequency.PERIODIC, 1),
    ],
)
async def test_create_series_rejects_invalid_frequency_interval(
    frequency: SeriesFrequency,
    interval_days: int | None,
) -> None:
    service = make_service(FakeTaskManagementRepository())
    with pytest.raises(BadRequestError):
        await service.create(
            USER_ID,
            title="Invalid",
            description=None,
            priority=Priority.NEITHER,
            frequency=frequency,
            interval_days=interval_days,
            time_of_day=time(9, 0),
            timezone="UTC",
            starts_on=date(2026, 11, 4),
            ends_on=None,
            project_id=None,
            label_ids=(),
            reminders_enabled=False,
            reminders=(),
        )


async def test_update_periodic_interval_regenerates_future_instances() -> None:
    """Changing a periodic interval replaces future phase slots."""
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

    updated = await service.update(USER_ID, series.id, interval_days=4)

    assert updated.frequency == "periodic"
    assert updated.interval_days == 4
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
    assert generated[0].due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert all((task.due_at.date() - date(2026, 11, 1)).days % 4 == 0 for task in generated)


async def test_update_daily_to_periodic_regenerates_future_instances() -> None:
    """Converting a daily series to periodic replaces future daily slots."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    updated = await service.update(
        USER_ID,
        series.id,
        frequency=SeriesFrequency.PERIODIC,
        interval_days=3,
    )

    assert updated.frequency == "periodic"
    assert updated.interval_days == 3
    assert future.deleted_at == fixed_now()
    generated = sorted(
        (
            task
            for task in repository.tasks.values()
            if task.series_id == series.id and task.id != future.id
        ),
        key=lambda task: task.due_at,
    )
    assert generated
    assert generated[0].due_at == datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


async def test_update_periodic_to_daily_normalizes_interval() -> None:
    """Converting a periodic series to daily resets the interval to one."""
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
    future = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    updated = await service.update(USER_ID, series.id, frequency=SeriesFrequency.DAILY)

    assert updated.frequency == "daily"
    assert updated.interval_days == 1
    assert future.deleted_at == fixed_now()
    generated = sorted(
        (
            task
            for task in repository.tasks.values()
            if task.series_id == series.id and task.id != future.id
        ),
        key=lambda task: task.due_at,
    )
    assert generated
    assert generated[0].due_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


async def test_update_interval_only_rejects_daily_series_with_new_interval() -> None:
    """A daily series cannot adopt a periodic interval without the frequency."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, series.id, interval_days=3)

    assert series.frequency == "daily"
    assert series.interval_days == 1


async def test_update_interval_only_allows_daily_no_op() -> None:
    """Passing the existing daily interval keeps the schedule untouched."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    updated = await service.update(USER_ID, series.id, interval_days=1)

    assert updated.frequency == "daily"
    assert updated.interval_days == 1
    assert future.deleted_at is None
    assert list(repository.tasks) == [future.id]


async def test_update_interval_only_keeps_periodic_frequency() -> None:
    """An interval-only update preserves the periodic frequency."""
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
    service = make_service(repository)

    updated = await service.update(USER_ID, series.id, interval_days=5)

    assert updated.frequency == "periodic"
    assert updated.interval_days == 5


async def test_generate_series_uses_stored_interval() -> None:
    """Repeated generation runs keep periodic instances on the stored phase."""
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
    service = make_service(repository)

    await service.generate(fixed_now())
    await service.generate(fixed_now() + timedelta(days=30))
    await service.generate(fixed_now() + timedelta(days=60))

    tasks = [task for task in repository.tasks.values() if task.series_id == series.id]
    assert tasks
    assert all((task.due_at.date() - date(2026, 11, 1)).days % 3 == 0 for task in tasks)
    assert len({task.due_at for task in tasks}) == len(tasks)


async def test_create_from_task_snapshots_and_does_not_duplicate_due_slot() -> None:
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Moscow"
    project = repository.insert_project(make_project())
    label = repository.insert_label(make_label())
    due_at = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    task = repository.insert_task(
        make_task(
            due_at=due_at,
            project_id=project.id,
            title="Snapshot",
            description="Description",
            priority=Priority.URGENT_IMPORTANT.value,
            reminders_enabled=True,
        )
    )
    repository.task_labels.add((task.id, label.id))
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=due_at,
        )
    )
    service = make_service(repository)

    series = await service.create_from_task(
        USER_ID,
        task,
        frequency=SeriesFrequency.PERIODIC,
        interval_days=3,
    )

    assert task.series_id == series.id
    assert series.starts_on == date(2026, 11, 7)
    assert series.time_of_day == time(9, 0)
    assert series.timezone == "Europe/Moscow"
    assert series.interval_days == 3
    assert series.reminders_enabled is True
    assert sum(1 for item in repository.tasks.values() if item.due_at == due_at) == 1
    templates = await repository.list_series_reminders(USER_ID, series.id)
    assert templates[0].interval_days == 3
    instance_reminders = await repository.list_task_reminders(USER_ID, task.id)
    assert instance_reminders[0].interval_days == 1


async def test_create_from_task_rejects_archived_project() -> None:
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
        await service.create_from_task(
            USER_ID,
            task,
            frequency=SeriesFrequency.DAILY,
        )


async def test_create_from_task_regular_preserves_reminder_interval() -> None:
    """Converting to daily keeps the current scheduled reminder interval."""
    repository = FakeTaskManagementRepository()
    due_at = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
    task = repository.insert_task(make_task(due_at=due_at, reminders_enabled=True))
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=2,
            time_of_day=time(6, 0),
            timezone="UTC",
            next_run_at=due_at,
        )
    )
    service = make_service(repository)

    series = await service.create_from_task(
        USER_ID,
        task,
        frequency=SeriesFrequency.DAILY,
    )

    templates = await repository.list_series_reminders(USER_ID, series.id)
    assert series.frequency == "daily"
    assert series.interval_days == 1
    assert templates[0].interval_days == 2


async def test_detach_task_hard_deletes_future_siblings_except_current() -> None:
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    past = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() - timedelta(days=1))
    )
    current = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    earlier_future = repository.insert_task(
        make_task(task_id=3, series_id=series.id, due_at=fixed_now() + timedelta(hours=1))
    )
    later_future = repository.insert_task(
        make_task(task_id=4, series_id=series.id, due_at=fixed_now() + timedelta(days=2))
    )
    completed = repository.insert_task(
        make_task(
            task_id=5,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=3),
            completed_at=fixed_now(),
        )
    )
    overdue = repository.insert_task(
        make_task(task_id=6, series_id=series.id, due_at=fixed_now() - timedelta(hours=1))
    )
    service = make_service(repository)

    await service.detach_task(USER_ID, current, series)

    assert series.deleted_at == fixed_now()
    assert current.series_id is None
    assert {past.id, current.id, completed.id, overdue.id} <= set(repository.tasks)
    assert earlier_future.id not in repository.tasks
    assert later_future.id not in repository.tasks


async def test_detach_task_keeps_current_completed_task() -> None:
    """A completed current task survives detach as a one-off."""
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
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now(),
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

    await service.detach_task(USER_ID, current, series)

    assert current.id in repository.tasks
    assert current.series_id is None
    assert future.id not in repository.tasks
    assert series.deleted_at == fixed_now()


async def test_detach_task_clears_deleted_task_related_rows() -> None:
    """Hard-deleted siblings take their labels, reminders, and deliveries with them."""
    repository = FakeTaskManagementRepository()
    label = repository.insert_label(make_label())
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
            due_at=fixed_now() + timedelta(hours=1),
        )
    )
    removed = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    repository.task_labels.update({(current.id, label.id), (removed.id, label.id)})
    removed_reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=removed.id,
            next_run_at=fixed_now(),
        )
    )
    current_reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=2,
            task_id=current.id,
            next_run_at=fixed_now(),
        )
    )
    removed_delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=removed_reminder.id,
            scheduled_for=fixed_now(),
        )
    )
    current_delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=2,
            reminder_id=current_reminder.id,
            scheduled_for=fixed_now(),
        )
    )
    service = make_service(repository)

    await service.detach_task(USER_ID, current, series)

    assert removed.id not in repository.tasks
    assert (removed.id, label.id) not in repository.task_labels
    assert removed_reminder.id not in repository.reminders
    assert removed_delivery.id not in repository.deliveries
    assert current.id in repository.tasks
    assert (current.id, label.id) in repository.task_labels
    assert current_reminder.id in repository.reminders
    assert current_delivery.id in repository.deliveries


async def test_create_from_task_flushes_series_link_before_due_slot_dedupe() -> None:
    """The converted task must be persisted before generation deduplicates slots."""
    repository = FlushOrderingRepository()
    task = repository.insert_task(make_task(due_at=datetime(2026, 11, 7, 6, 0, tzinfo=UTC)))
    repository.watched_task = task
    service = make_service(repository)

    series = await service.create_from_task(
        USER_ID,
        task,
        frequency=SeriesFrequency.DAILY,
    )

    assert task.series_id == series.id
    assert repository.task_flushed_before_due_lookup is True


async def test_create_from_task_rejects_derived_start_outside_supported_range() -> None:
    """A local due date outside the supported bounds is rejected before writes."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Pacific/Honolulu"
    task = repository.insert_task(make_task(due_at=datetime(2025, 1, 1, 0, 0, tzinfo=UTC)))
    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.create_from_task(
            USER_ID,
            task,
            frequency=SeriesFrequency.DAILY,
        )

    assert repository.series == {}
    assert task.series_id is None
