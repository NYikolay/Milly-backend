from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from backend.models.task import Task
from backend.models.task_reminder import TaskReminder
from backend.task_management.enums import Priority, ReminderKind
from backend.task_management.reminders import (
    ReminderContent,
    ReminderDispatcher,
    build_reminder_text,
    next_reminder_slot,
)
from tests.task_management_fakes import (
    FakeSender,
    FakeTaskManagementRepository,
    fixed_now,
    make_delivery,
    make_project,
    make_reminder,
    make_series,
    make_task,
)

USER_ID = 7
TELEGRAM_ID = 7007


def make_dispatcher(
    repository: FakeTaskManagementRepository,
    sender: FakeSender,
) -> ReminderDispatcher:
    """Build a reminder dispatcher with the fixed clock."""
    return ReminderDispatcher(repository, sender, clock=fixed_now)


def seed_telegram(repository: FakeTaskManagementRepository) -> None:
    """Link a distinct Telegram chat identifier to the test user."""
    repository.telegram_ids[USER_ID] = TELEGRAM_ID


def make_scheduled_reminder(
    repository: FakeTaskManagementRepository,
) -> tuple[Task, TaskReminder]:
    """Seed a due scheduled reminder and return its task and reminder."""
    seed_telegram(repository)
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


def make_content(
    *,
    title: str = "Task Title",
    description: str | None = None,
    priority: str = Priority.NEITHER.value,
    due_at: datetime | None = None,
    project_name: str | None = None,
) -> ReminderContent:
    """Build reminder content with deterministic defaults."""
    return ReminderContent(
        title=title,
        description=description,
        priority=priority,
        due_at=due_at,
        project_name=project_name,
    )


def test_reminder_text_renders_full_card() -> None:
    """A scheduled reminder renders title, description, due, priority, and project."""
    content = make_content(
        title="Купить молоко",
        description="Взять 2 литра",
        priority=Priority.URGENT_IMPORTANT.value,
        due_at=datetime(2026, 11, 5, 18, 0, tzinfo=UTC),
        project_name="Дом",
    )

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="UTC",
    )

    assert text == (
        "⏰ Напоминание о задаче\n"
        "\n"
        "📝 <b>Купить молоко</b>\n"
        "Взять 2 литра\n"
        "\n"
        "📅 Завтра в 18:00\n"
        "🔴 Срочные и важные\n"
        "📁 Дом"
    )


def test_reminder_text_omits_missing_optional_fields() -> None:
    """Empty description, default priority, and no project leave no blank lines."""
    content = make_content(due_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC))

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="UTC",
    )

    assert text == ("⏰ Напоминание о задаче\n\n📝 <b>Task Title</b>\n\n📅 Завтра в 12:00")


def test_reminder_text_escapes_user_html() -> None:
    """User-provided title and description are escaped for Telegram HTML."""
    content = make_content(
        title="<b>Важно</b> & срочно",
        description="1 < 2 & 3 > 2",
        due_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
        project_name="R&D",
    )

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="UTC",
    )

    assert "📝 <b>&lt;b&gt;Важно&lt;/b&gt; &amp; срочно</b>" in text
    assert "1 &lt; 2 &amp; 3 &gt; 2" in text
    assert "📁 R&amp;D" in text


def test_reminder_text_truncates_long_description() -> None:
    """A long description is cut to the display limit with an ellipsis."""
    description = "д" * 250
    content = make_content(
        description=description,
        due_at=datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
    )

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="UTC",
    )

    assert "д" * 200 + "…" in text
    assert "д" * 201 not in text


def test_reminder_text_formats_due_in_reminder_timezone() -> None:
    """The due date is rendered in the reminder timezone, not UTC."""
    content = make_content(due_at=datetime(2026, 11, 4, 18, 0, tzinfo=UTC))

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="Europe/Moscow",
    )

    assert "📅 Сегодня в 21:00" in text


def test_reminder_text_formats_far_due_date_with_month() -> None:
    """A due date beyond tomorrow uses the day and month name."""
    content = make_content(due_at=datetime(2026, 12, 15, 10, 30, tzinfo=UTC))

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="UTC",
    )

    assert "📅 15 декабря в 10:30" in text


def test_reminder_text_omits_due_line_without_due_at() -> None:
    """A series reminder without a due instant renders only the title block."""
    content = make_content(title="Daily", due_at=None)

    text = build_reminder_text(
        content,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        now=fixed_now(),
        timezone_name="UTC",
    )

    assert text == "⏰ Напоминание о задаче\n\n📝 <b>Daily</b>"


@pytest.mark.parametrize(
    ("offset_minutes", "expected"),
    [
        (1, "⏳ До дедлайна 1 минута"),
        (2, "⏳ До дедлайна 2 минуты"),
        (5, "⏳ До дедлайна 5 минут"),
        (30, "⏳ До дедлайна 30 минут"),
        (60, "⏳ До дедлайна 1 час"),
        (90, "⏳ До дедлайна 1 час 30 минут"),
        (120, "⏳ До дедлайна 2 часа"),
        (1440, "⏳ До дедлайна 1 день"),
        (2880, "⏳ До дедлайна 2 дня"),
        (7200, "⏳ До дедлайна 5 дней"),
    ],
)
def test_reminder_text_before_due_shows_remaining_time(
    offset_minutes: int,
    expected: str,
) -> None:
    """A before-due reminder shows the remaining time with Russian plurals."""
    content = make_content(due_at=fixed_now() + timedelta(minutes=offset_minutes))

    text = build_reminder_text(
        content,
        kind=ReminderKind.BEFORE_DUE,
        offset_minutes=offset_minutes,
        now=fixed_now(),
        timezone_name=None,
    )

    assert expected in text


def test_reminder_text_before_due_marks_passed_deadline() -> None:
    """A before-due reminder sent after the deadline says the deadline passed."""
    content = make_content(due_at=fixed_now() - timedelta(minutes=5))

    text = build_reminder_text(
        content,
        kind=ReminderKind.BEFORE_DUE,
        offset_minutes=30,
        now=fixed_now(),
        timezone_name=None,
    )

    assert "⏳ Дедлайн наступил" in text


def scheduled_text(title: str, due: str = "Завтра в 12:00") -> str:
    """Render the expected scheduled reminder message for a fixture."""
    return f"⏰ Напоминание о задаче\n\n📝 <b>{title}</b>\n\n📅 {due}"


async def test_dispatch_renders_task_details_in_message() -> None:
    """The dispatcher renders description, priority, and project for a task."""
    repository = FakeTaskManagementRepository()
    seed_telegram(repository)
    project = repository.insert_project(make_project(project_id=1, name="Дом"))
    task = repository.insert_task(
        make_task(
            task_id=1,
            project_id=project.id,
            title="Купить молоко",
            description="Взять 2 литра",
            priority=Priority.URGENT_IMPORTANT.value,
            due_at=datetime(2026, 11, 5, 18, 0, tzinfo=UTC),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            task_id=task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert sender.calls == [
        (
            TELEGRAM_ID,
            "⏰ Напоминание о задаче\n"
            "\n"
            "📝 <b>Купить молоко</b>\n"
            "Взять 2 литра\n"
            "\n"
            "📅 Завтра в 18:00\n"
            "🔴 Срочные и важные\n"
            "📁 Дом",
        )
    ]


async def test_dispatch_renders_series_reminder_without_due_line() -> None:
    """A series reminder without a due instant renders only the title block."""
    repository = FakeTaskManagementRepository()
    seed_telegram(repository)
    series = repository.insert_series(
        make_series(
            series_id=1,
            title="Daily",
            time_of_day=time(9, 0),
            starts_on=fixed_now().date(),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=1,
            series_id=series.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 0),
            timezone="UTC",
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert sender.calls == [(TELEGRAM_ID, "⏰ Напоминание о задаче\n\n📝 <b>Daily</b>")]


def test_next_reminder_slot_jumps_over_many_intervals() -> None:
    """Advance a long-stale scheduled reminder without stepping interval by interval."""
    reminder = make_reminder(
        reminder_id=1,
        task_id=1,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        interval_days=1,
        time_of_day=time(9, 0),
        timezone="UTC",
    )
    result = next_reminder_slot(
        reminder,
        current=datetime(2025, 1, 1, 9, 0, tzinfo=UTC),
        now=datetime(2100, 12, 1, tzinfo=UTC),
    )
    assert result is not None
    assert result > datetime(2100, 12, 1, tzinfo=UTC)


def test_next_reminder_slot_returns_the_first_slot_after_current() -> None:
    """Do not skip an interval that is still ahead of the current instant."""
    reminder = make_reminder(
        reminder_id=1,
        task_id=1,
        kind=ReminderKind.SCHEDULED,
        offset_minutes=None,
        interval_days=1,
        time_of_day=time(9, 0),
        timezone="UTC",
    )
    result = next_reminder_slot(
        reminder,
        current=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        now=datetime(2026, 11, 5, 8, 0, tzinfo=UTC),
    )
    assert result == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


async def test_dispatch_creates_keyed_delivery_sends_and_advances_slot() -> None:
    """A due reminder creates one delivery, sends text, and advances the slot."""
    repository = FakeTaskManagementRepository()
    task, reminder = make_scheduled_reminder(repository)
    sender = FakeSender(message_id=321)
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert sender.calls == [(TELEGRAM_ID, scheduled_text("Task Title"))]
    assert USER_ID != TELEGRAM_ID
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


async def test_dispatch_due_passes_batch_limit() -> None:
    """Dispatch asks the repository for at most one bounded batch."""
    repository = FakeTaskManagementRepository()
    make_scheduled_reminder(repository)
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    await dispatcher.dispatch_due()

    assert repository.due_reminder_limits == [100]


async def test_dispatch_due_stops_at_deadline() -> None:
    """A run that reaches the soft deadline leaves every reminder for the next run."""
    repository = FakeTaskManagementRepository()
    make_scheduled_reminder(repository)
    sender = FakeSender()
    ticks = iter([0.0] + [240.0] * 10)
    dispatcher = ReminderDispatcher(
        repository,
        sender,
        clock=fixed_now,
        monotonic=lambda: next(ticks),
    )

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    assert repository.deliveries == {}


async def test_dispatch_due_finishes_in_flight_send() -> None:
    """Crossing the deadline stops the loop only after the current send completes."""
    repository = FakeTaskManagementRepository()
    _, first_reminder = make_scheduled_reminder(repository)
    second_task = repository.insert_task(
        make_task(
            task_id=2,
            title="Second",
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=2,
            task_id=second_task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 30),
            timezone="UTC",
            next_run_at=datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
        )
    )
    claims: list[int] = []
    repository.on_claim = lambda: claims.append(1)
    sender = FakeSender()
    ticks = iter([0.0, 239.9, 250.0, 250.0])
    dispatcher = ReminderDispatcher(
        repository,
        sender,
        clock=fixed_now,
        monotonic=lambda: next(ticks),
    )

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert claims == [1]
    assert sender.calls == [(TELEGRAM_ID, scheduled_text("Task Title"))]
    assert repository.deliveries[1].reminder_id == first_reminder.id
    assert 2 not in repository.deliveries


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
    seed_telegram(repository)
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
    seed_telegram(repository)
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
    seed_telegram(repository)
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


async def test_missing_telegram_profile_fails_delivery_without_sending() -> None:
    """A user without a Telegram profile fails the slot without calling the sender."""
    repository = FakeTaskManagementRepository()
    _, reminder = make_scheduled_reminder(repository)
    repository.telegram_ids.clear()
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    delivery = repository.deliveries[1]
    assert delivery.status == "failed"
    assert delivery.locked_at is None
    assert delivery.last_error == "Telegram profile is missing"
    assert reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


async def test_unexpected_failure_keeps_earlier_delivery_and_continues() -> None:
    """A later unexpected error rolls back only that reminder and keeps earlier sends."""
    repository = FakeTaskManagementRepository()
    first_task, first_reminder = make_scheduled_reminder(repository)
    second_task = repository.insert_task(
        make_task(
            task_id=2,
            title="Second",
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    repository.insert_reminder(
        make_reminder(
            reminder_id=2,
            task_id=second_task.id,
            kind=ReminderKind.SCHEDULED,
            offset_minutes=None,
            interval_days=1,
            time_of_day=time(9, 30),
            timezone="UTC",
            next_run_at=datetime(2026, 11, 4, 9, 30, tzinfo=UTC),
        )
    )
    repository.task_errors.add(second_task.id)
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert sender.calls == [(TELEGRAM_ID, scheduled_text("Task Title"))]
    assert repository.deliveries[1].status == "sent"
    assert first_reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    assert repository.rollbacks == 1
    assert 2 not in repository.deliveries


async def test_archived_project_after_claim_cancels_delivery_without_sending() -> None:
    """An archive that lands between listing and claiming cancels the delivery."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(project_id=1))
    seed_telegram(repository)
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
            kind=ReminderKind.BEFORE_DUE,
            offset_minutes=30,
            next_run_at=fixed_now() - timedelta(minutes=1),
        )
    )
    repository.on_claim = lambda: setattr(project, "archived_at", fixed_now())
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    delivery = repository.deliveries[1]
    assert delivery.status == "cancelled"
    assert delivery.locked_at is None
    assert delivery.next_attempt_at is None
    assert reminder.next_run_at is None


async def test_expired_lease_is_reclaimed_and_sent() -> None:
    """A sending delivery with an expired lease can be claimed by another worker."""
    repository = FakeTaskManagementRepository()
    _, reminder = make_scheduled_reminder(repository)
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            status="sending",
            attempts=1,
            locked_at=fixed_now() - timedelta(minutes=6),
        )
    )
    sender = FakeSender(message_id=77)
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 1
    assert delivery.status == "sent"
    assert delivery.attempts == 2
    assert delivery.telegram_message_id == 77


async def test_pending_delivery_with_future_retry_is_skipped() -> None:
    """A pending delivery waiting for its backoff is not sent early."""
    repository = FakeTaskManagementRepository()
    _, reminder = make_scheduled_reminder(repository)
    delivery = repository.insert_delivery(
        make_delivery(
            delivery_id=1,
            reminder_id=reminder.id,
            scheduled_for=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
            status="pending",
            attempts=1,
            next_attempt_at=fixed_now() + timedelta(minutes=5),
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    assert delivery.status == "pending"
    assert delivery.next_attempt_at == fixed_now() + timedelta(minutes=5)
    assert reminder.next_run_at == datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


async def test_rollback_of_middle_candidate_still_processes_third() -> None:
    """A rollback for one candidate does not abort the remaining loop."""
    repository = FakeTaskManagementRepository()
    seed_telegram(repository)
    first = repository.insert_task(
        make_task(
            task_id=1,
            title="First",
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    second = repository.insert_task(
        make_task(
            task_id=2,
            title="Second",
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    third = repository.insert_task(
        make_task(
            task_id=3,
            title="Third",
            due_at=fixed_now() + timedelta(days=1),
            reminders_enabled=True,
        )
    )
    for index, task in enumerate((first, second, third), start=1):
        repository.insert_reminder(
            make_reminder(
                reminder_id=index,
                task_id=task.id,
                kind=ReminderKind.SCHEDULED,
                offset_minutes=None,
                interval_days=1,
                time_of_day=time(9 + index, 0),
                timezone="UTC",
                next_run_at=datetime(2026, 11, 4, 9 + index, 0, tzinfo=UTC),
            )
        )
    repository.task_errors.add(second.id)
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 2
    assert sender.calls == [
        (TELEGRAM_ID, scheduled_text("First")),
        (TELEGRAM_ID, scheduled_text("Third")),
    ]
    assert repository.rollbacks == 1
    assert sorted(delivery.reminder_id for delivery in repository.deliveries.values()) == [1, 3]


async def test_disabled_series_toggle_cancels_instance_delivery() -> None:
    """An overdue instance reminder stops when its parent series toggle is off."""
    repository = FakeTaskManagementRepository()
    seed_telegram(repository)
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=fixed_now().date(),
            reminders_enabled=False,
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
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
            next_run_at=fixed_now() - timedelta(minutes=5),
        )
    )
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == []
    assert repository.deliveries[1].status == "cancelled"
    assert reminder.next_run_at is None


async def test_sent_mark_is_committed_before_slot_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slot update failure after sending does not cause a duplicate message."""
    repository = FakeTaskManagementRepository()
    _, reminder = make_scheduled_reminder(repository)
    original_update = repository.update_reminder_next_run_at
    calls = 0

    async def flaky_update(
        target: TaskReminder,
        next_run_at: datetime | None,
    ) -> None:
        """Fail the first slot update and accept later attempts."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("slot update failed")
        await original_update(target, next_run_at)

    monkeypatch.setattr(repository, "update_reminder_next_run_at", flaky_update)
    sender = FakeSender()
    dispatcher = make_dispatcher(repository, sender)

    sent = await dispatcher.dispatch_due()

    assert sent == 0
    assert sender.calls == [(TELEGRAM_ID, scheduled_text("Task Title"))]
    assert repository.deliveries[1].status == "sent"
    assert repository.rollbacks == 1

    resumed = await dispatcher.dispatch_due()

    assert resumed == 0
    assert len(sender.calls) == 1
    assert reminder.next_run_at == datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
