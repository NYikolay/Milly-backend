from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from backend.models.task import Task
from backend.models.task_reminder import TaskReminder
from backend.task_management.enums import ReminderKind
from backend.task_management.reminders import ReminderDispatcher
from tests.task_management_fakes import (
    FakeSender,
    FakeTaskManagementRepository,
    fixed_now,
    make_delivery,
    make_project,
    make_reminder,
    make_task,
)

USER_ID = 7


def make_dispatcher(
    repository: FakeTaskManagementRepository,
    sender: FakeSender,
) -> ReminderDispatcher:
    """Build a reminder dispatcher with the fixed clock."""
    return ReminderDispatcher(repository, sender, clock=fixed_now)


def make_scheduled_reminder(
    repository: FakeTaskManagementRepository,
) -> tuple[Task, TaskReminder]:
    """Seed a due scheduled reminder and return its task and reminder."""
    task = repository.insert_task(
        make_task(
            task_id=1,
            title="Task Title",
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    reminder = repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        )
    )
    return task, reminder


async def test_dispatch_creates_keyed_delivery_sends_and_advances_slot() -> None:
    """A due reminder creates one delivery, sends text, and advances the slot."""
    repository = FakeTaskManagementRepository()
    task, reminder = make_scheduled_reminder(repository)
    sender = FakeSender(message_id=321)
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert sender.calls == [(USER_ID, "Напоминание: Task Title")]
    delivery = repository.deliveries[1]
    assert (delivery.reminder_id, delivery.scheduled_for) == (
        reminder.id,
        datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
    )
    assert delivery.status == "sent"
    assert delivery.sent_at == fixed_now()
    assert delivery.telegram_message_id == 321
    assert delivery.locked_at is None
    assert reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert repository.commits >= 1


async def test_dispatch_skips_sent_key_and_only_advances_slot() -> None:
    """An already sent key is never sent twice."""
    repository = FakeTaskManagementRepository()
    task, reminder = make_scheduled_reminder(repository)
    existing = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            status="sent",
            attempts=1,
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    assert existing.status == "sent"
    assert reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


async def test_dispatch_reactivates_cancelled_delivery() -> None:
    """A cancelled delivery for the current slot is reactivated instead of duplicated."""
    repository = FakeTaskManagementRepository()
    task, reminder = make_scheduled_reminder(repository)
    cancelled = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            status="cancelled",
            attempts=2,
            locked_at=fixed_now(),
        )
    )
    sender = FakeSender(message_id=9)
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert len(repository.deliveries) == 1
    assert cancelled.status == "sent"
    assert cancelled.attempts == 1
    assert cancelled.telegram_message_id == 9


async def test_before_due_success_clears_next_run() -> None:
    """A sent before-due reminder is final and clears its slot."""
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
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert reminder.next_run_at is None
    assert repository.deliveries[1].status == "sent"


async def test_sender_failure_marks_pending_with_backoff_and_clears_lock() -> None:
    """A retryable Telegram failure leaves a pending delivery with backoff."""
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
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    sender = FakeSender(error=RuntimeError("telegram is down"))
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    delivery = repository.deliveries[1]
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    assert delivery.locked_at is None
    assert delivery.next_attempt_at is not None
    assert delivery.next_attempt_at > fixed_now()
    assert delivery.last_error == "RuntimeError"
    assert reminder.next_run_at == fixed_now() - timedelta(minutes=1)


async def test_final_failure_marks_failed_and_clears_before_due_slot() -> None:
    """The fifth failed attempt is final and clears a before-due slot."""
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
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=fixed_now() - timedelta(minutes=1),
            status="pending",
            attempts=4,
        )
    )
    sender = FakeSender(error=RuntimeError("telegram is down"))
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert delivery.status == "failed"
    assert delivery.attempts == 5
    assert delivery.locked_at is None
    assert reminder.next_run_at is None


async def test_dispatch_ignores_disabled_master_toggle_and_archived_projects() -> None:
    """Archived projects and disabled master toggles produce no deliveries."""
    repository = FakeTaskManagementRepository()
    archived = repository.insert_project(make_project(project_id=1, archived_at=fixed_now()))
    repository.insert_task(
        make_task(
            task_id=1,
            project_id=archived.id,
            due_at=fixed_now() + timedelta(hours=1),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=1,
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    repository.insert_task(
        make_task(
            task_id=2,
            title="Disabled",
            due_at=fixed_now() + timedelta(hours=1),
            reminders_enabled=False,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=2,
            task_id=2,
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    assert repository.deliveries == {}
