from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from backend.application.task_management.series import SeriesService
from backend.application.task_management.series_materializer import SeriesMaterializer
from backend.application.task_management.series_reminders import build_instance_reminder
from backend.application.task_management.task_references import TaskReferences
from backend.core.errors import BadRequestError
from backend.domain.enums import Priority, ReminderKind
from backend.domain.task_management.models import ReminderInput
from tests.task_management_fakes import (
    FakeTaskManagementRepository,
    fixed_now,
    make_label,
    make_project,
    make_reminder,
    make_series,
)

USER_ID = 7


def make_materializer(repository: FakeTaskManagementRepository) -> SeriesMaterializer:
    """Build a real series materializer over the fake repository ports."""
    return SeriesMaterializer(
        repository,
        repository,
        repository,
        repository,
        TaskReferences(repository, repository, repository),
        repository,
    )


def make_series_service(repository: FakeTaskManagementRepository) -> SeriesService:
    """Build a real series service over the fake repository ports."""
    references = TaskReferences(repository, repository, repository)
    return SeriesService(
        repository,
        repository,
        repository,
        references,
        make_materializer(repository),
        repository,
        clock=fixed_now,
    )


async def test_materializer_creates_tasks_labels_and_instance_reminders() -> None:
    """One series materializes the same tasks, label rows, and reminders as before."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    label = repository.insert_label(make_label())
    service = make_series_service(repository)

    series = await service.create(
        USER_ID,
        title="Daily",
        description=None,
        priority=Priority.NEITHER,
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=date(2026, 11, 1),
        ends_on=None,
        project_id=project.id,
        label_ids=(label.id,),
        reminders_enabled=True,
        reminders=(ReminderInput(kind=ReminderKind.BEFORE_DUE, offset_minutes=30),),
    )

    tasks = sorted(
        (task for task in repository.tasks.values() if task.series_id == series.id),
        key=lambda task: task.due_at,
    )
    assert series.generated_through is not None
    assert tasks
    assert repository.task_labels == {(task.id, label.id) for task in tasks}
    for task in tasks:
        instance_reminders = await repository.list_task_reminders(USER_ID, task.id)
        assert [reminder.next_run_at for reminder in instance_reminders] == [
            task.due_at - timedelta(minutes=30)
        ]


async def test_materializer_skips_archived_project_without_writes() -> None:
    """An archived project yields zero tasks and no materialization writes."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    label = repository.insert_label(make_label())
    series = repository.insert_series(
        make_series(
            series_id=1,
            project_id=project.id,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date(),
        )
    )
    repository.series_labels.add((series.id, label.id))
    materializer = make_materializer(repository)

    created = await materializer.materialize(series, fixed_now())

    assert created == 0
    assert repository.tasks == {}
    assert repository.task_labels == set()
    assert repository.bulk_calls == []
    assert repository.saved_series == []
    assert repository.flush_calls == 0


def test_build_instance_reminder_matches_before_due_daily_and_periodic_rules() -> None:
    """Instance reminders keep the before-due, daily, and periodic slot rules."""
    due_at = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    before_due = make_reminder(
        kind=ReminderKind.BEFORE_DUE,
        offset_minutes=30,
    )
    scheduled = make_reminder(
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        interval_days=1,
        time_of_day=time(8, 0),
        timezone="UTC",
    )

    reminder = build_instance_reminder(
        before_due,
        task_id=11,
        due_at=due_at,
        fallback_timezone="UTC",
        is_periodic=False,
        now=fixed_now(),
    )
    assert reminder is not None
    assert reminder.next_run_at == datetime(2026, 11, 6, 8, 30, tzinfo=UTC)
    assert (
        build_instance_reminder(
            before_due,
            task_id=12,
            due_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            fallback_timezone="UTC",
            is_periodic=False,
            now=fixed_now(),
        )
        is None
    )

    daily = build_instance_reminder(
        scheduled,
        task_id=13,
        due_at=due_at,
        fallback_timezone="UTC",
        is_periodic=False,
        now=fixed_now(),
    )
    assert daily is not None
    assert daily.next_run_at == datetime(2026, 11, 7, 8, 0, tzinfo=UTC)

    periodic = build_instance_reminder(
        scheduled,
        task_id=14,
        due_at=due_at,
        fallback_timezone="UTC",
        is_periodic=True,
        now=fixed_now(),
    )
    assert periodic is not None
    assert periodic.next_run_at == datetime(2026, 11, 6, 8, 0, tzinfo=UTC)


def test_build_instance_reminder_translates_reminder_configuration_errors() -> None:
    """A reminder schedule overflow becomes a bad-request error."""
    template = make_reminder(
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        interval_days=1,
        time_of_day=time(0, 0),
        timezone="UTC",
    )

    with pytest.raises(BadRequestError):
        build_instance_reminder(
            template,
            task_id=1,
            due_at=datetime.max.replace(tzinfo=UTC),
            fallback_timezone="UTC",
            is_periodic=False,
            now=fixed_now(),
        )
