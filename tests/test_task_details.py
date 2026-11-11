from __future__ import annotations

from datetime import date, time, timedelta

from backend.application.task_management.task_details import TaskDetails
from backend.domain.enums import TaskRecurrence
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


def make_details(repository: FakeTaskManagementRepository) -> TaskDetails:
    """Build task detail assembly over the fake repository ports."""
    return TaskDetails(repository, repository, repository)


async def test_detail_includes_labels_reminders_project_and_recurrence() -> None:
    """A single task detail carries every related collection."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(project_id=1))
    label = repository.insert_label(make_label(label_id=1))
    series = repository.insert_series(
        make_series(
            series_id=1,
            frequency="daily",
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            project_id=project.id,
            series_id=series.id,
            due_at=fixed_now() + timedelta(hours=1),
        )
    )
    repository.task_labels.add((task.id, label.id))
    reminder = repository.insert_reminder(make_reminder(reminder_id=1, task_id=task.id))
    details = make_details(repository)

    detail = await details.detail(USER_ID, task)

    assert detail.task is task
    assert list(detail.labels) == [label]
    assert list(detail.reminders) == [reminder]
    assert detail.project is project
    assert detail.recurrence is TaskRecurrence.REGULAR


async def test_details_batches_labels_and_reminders_once_for_multiple_tasks() -> None:
    """A list detail page uses one label and one reminder batch query."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(project_id=1))
    label = repository.insert_label(make_label(label_id=1))
    daily = repository.insert_series(
        make_series(
            series_id=1,
            frequency="daily",
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
    tasks = [
        repository.insert_task(
            make_task(
                task_id=task_id,
                project_id=project.id,
                series_id=series_id,
                due_at=fixed_now() + timedelta(minutes=task_id),
            )
        )
        for task_id, series_id in ((1, daily.id), (2, periodic.id), (3, None))
    ]
    repository.task_labels.add((1, label.id))
    repository.task_labels.add((2, label.id))
    for task_id in (1, 2, 3):
        repository.insert_reminder(
            make_reminder(reminder_id=task_id, task_id=task_id),
        )
    details = make_details(repository)

    items = await details.details(USER_ID, tasks)

    assert [item.task.id for item in items] == [1, 2, 3]
    assert [item.recurrence for item in items] == [
        TaskRecurrence.REGULAR,
        TaskRecurrence.PERIODIC,
        TaskRecurrence.ONE_OFF,
    ]
    assert [item.project for item in items] == [project, project, project]
    assert [len(item.labels) for item in items] == [1, 1, 0]
    assert [len(item.reminders) for item in items] == [1, 1, 1]
    batch_names = [name for name, _ in repository.batch_calls]
    assert batch_names.count("list_task_labels_for_tasks") == 1
    assert batch_names.count("list_task_reminders_for_tasks") == 1
    assert repository.batch_calls[0] == ("list_task_labels_for_tasks", (1, 2, 3))
    assert repository.batch_calls[1] == ("list_task_reminders_for_tasks", (1, 2, 3))
