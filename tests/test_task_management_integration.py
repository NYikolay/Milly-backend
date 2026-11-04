from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from backend.core.errors import NotFoundError
from backend.models.color_palette import ColorPalette
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.projects import ProjectService
from backend.task_management.reminders import ReminderDispatcher, ReminderInput
from backend.task_management.series import SeriesService
from backend.task_management.tasks import TaskFilters, TaskService
from tests.task_management_fakes import (
    FakeSender,
    FakeTaskManagementRepository,
    fixed_now,
)

USER_ID = 7
SECOND_REMINDER_AT = datetime(2026, 11, 6, 8, 30, tzinfo=UTC)
THIRD_REMINDER_AT = datetime(2026, 11, 7, 8, 30, tzinfo=UTC)
REMINDER_TEXT = "Напоминание: Daily review"


def make_services(
    repository: FakeTaskManagementRepository,
) -> tuple[ProjectService, SeriesService, TaskService]:
    """
    Build the real task-management services over the shared fake repository.

    Purpose:
        Exercise the same service composition used by the API and Taskiq jobs
        without PostgreSQL or a broker.

    Args:
        repository: In-memory repository double shared by all services.

    Returns:
        tuple[ProjectService, SeriesService, TaskService]: Project, series, and
            task services wired with the fixed test clock.
    """
    palette = PaletteService(repository)
    projects = ProjectService(repository, palette, clock=fixed_now)
    labels = LabelService(repository, palette, clock=fixed_now)
    series = SeriesService(repository, projects, labels, clock=fixed_now)
    tasks = TaskService(repository, series, clock=fixed_now)
    return projects, series, tasks


async def test_daily_series_lifecycle_end_to_end() -> None:
    """Drive a daily series through generation, reminders, archive, and delete."""
    repository = FakeTaskManagementRepository()
    projects, series_service, task_service = make_services(repository)

    color = repository.insert_color(
        ColorPalette(
            slug="ocean",
            name="Ocean",
            hex="#0A84FF",
            sort_order=2,
            is_active=True,
        )
    )
    project = await projects.create(USER_ID, name="Work", color_id=color.id)

    assert color.is_active is True
    assert project.color_id == color.id

    series = await series_service.create(
        USER_ID,
        title="Daily review",
        description="Review the day",
        priority=Priority.NEITHER,
        time_of_day=time(9, 0),
        timezone="UTC",
        starts_on=date(2026, 11, 4),
        ends_on=date(2026, 11, 7),
        project_id=project.id,
        label_ids=(),
        reminders_enabled=True,
        reminders=(ReminderInput(kind=ReminderKind.BEFORE_DUE, offset_minutes=30),),
    )
    tasks = sorted(
        (task for task in repository.tasks.values() if task.series_id == series.id),
        key=lambda task: task.due_at,
    )

    assert [task.due_at for task in tasks] == [
        datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 7, 9, 0, tzinfo=UTC),
    ]
    assert await series_service.generate(fixed_now()) == 0
    assert len(repository.tasks) == 3

    first, second, third = tasks
    completed = await task_service.complete(USER_ID, first.id)

    assert completed.task.completed_at == fixed_now()
    assert second.completed_at is None
    assert third.completed_at is None
    assert all(
        reminder.next_run_at is None
        for reminder in await repository.list_task_reminders(USER_ID, first.id)
    )
    assert [
        reminder.next_run_at
        for reminder in await repository.list_task_reminders(USER_ID, second.id)
    ] == [SECOND_REMINDER_AT]
    assert [
        reminder.next_run_at for reminder in await repository.list_task_reminders(USER_ID, third.id)
    ] == [THIRD_REMINDER_AT]

    success_sender = FakeSender(message_id=321)
    success_dispatcher = ReminderDispatcher(
        repository,
        success_sender,
        clock=lambda: SECOND_REMINDER_AT,
    )

    assert await success_dispatcher.dispatch_due() == 1
    assert success_sender.calls == [(USER_ID, REMINDER_TEXT)]
    deliveries = list(repository.deliveries.values())
    assert len(deliveries) == 1
    sent_delivery = deliveries[0]
    assert sent_delivery.status == "sent"
    assert sent_delivery.scheduled_for == SECOND_REMINDER_AT
    assert sent_delivery.telegram_message_id == 321

    assert await success_dispatcher.dispatch_due() == 0
    assert success_sender.calls == [(USER_ID, REMINDER_TEXT)]
    assert len(repository.deliveries) == 1

    archived = await projects.archive(USER_ID, project.id)

    assert archived.archived_at == fixed_now()
    for task in tasks:
        assert all(
            reminder.next_run_at is None
            for reminder in await repository.list_task_reminders(USER_ID, task.id)
        )
    assert await series_service.generate(fixed_now()) == 0
    assert len(repository.tasks) == 3
    paused_sender = FakeSender()
    paused_dispatcher = ReminderDispatcher(
        repository,
        paused_sender,
        clock=lambda: THIRD_REMINDER_AT,
    )
    assert await paused_dispatcher.dispatch_due() == 0
    assert paused_sender.calls == []
    assert len(repository.deliveries) == 1

    restored = await projects.unarchive(USER_ID, project.id)

    assert restored.archived_at is None
    third_reminder = (await repository.list_task_reminders(USER_ID, third.id))[0]
    assert third_reminder.next_run_at == THIRD_REMINDER_AT

    failure_sender = FakeSender(error=RuntimeError("telegram is down"))
    failure_dispatcher = ReminderDispatcher(
        repository,
        failure_sender,
        clock=lambda: THIRD_REMINDER_AT,
    )

    assert await failure_dispatcher.dispatch_due() == 0
    assert failure_sender.calls == [(USER_ID, REMINDER_TEXT)]
    pending = next(
        delivery
        for delivery in repository.deliveries.values()
        if delivery.reminder_id == third_reminder.id
    )
    assert pending.status == "pending"
    assert pending.attempts == 1

    await projects.delete(USER_ID, project.id)

    assert pending.status == "cancelled"
    assert pending.locked_at is None
    assert pending.next_attempt_at is None
    assert all(task.deleted_at == fixed_now() for task in tasks)
    assert series.deleted_at == fixed_now()
    assert await task_service.list(USER_ID, TaskFilters()) == []
    assert await series_service.list(USER_ID) == []
    with pytest.raises(NotFoundError):
        await projects.get(USER_ID, project.id)
    assert all(
        delivery.status in {"sent", "cancelled"} for delivery in repository.deliveries.values()
    )
