from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, cast
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.request import Request

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.auth.errors import AuthNotConfiguredError
from backend.core.config import Settings
from backend.infrastructure.telegram import sender as telegram_sender
from backend.infrastructure.telegram.sender import TelegramBotSender
from backend.tasks import delivery
from backend.tasks.delivery import dispatch_task_reminders


class FakeResponse:
    """Minimal urlopen response exposing a JSON body."""

    def __init__(self, body: bytes) -> None:
        """Store the response body."""
        self._body = body

    def __enter__(self) -> FakeResponse:
        """Enter the response context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the response context manager without suppressing errors."""
        return None

    def read(self) -> bytes:
        """Return the response body."""
        return self._body


def test_delivery_task_has_schedule() -> None:
    """The delivery task carries its every-minute cron label."""
    schedule = dispatch_task_reminders.labels["schedule"]
    assert schedule[0]["cron"] == "* * * * *"
    assert schedule[0]["schedule_id"] == "reminder-delivery"


async def test_delivery_task_delegates_to_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task body runs the dispatcher with configured settings and clock."""
    dispatched: list[None] = []
    constructed: dict[str, object] = {}

    class FakeDispatcher:
        """Reminder dispatcher double recording the dispatch call."""

        async def dispatch_due(self) -> int:
            """Record the dispatch and return a fixed count."""
            dispatched.append(None)
            return 2

    def fake_build_dispatcher(
        session: AsyncSession,
        sender: object,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> FakeDispatcher:
        """Record the composition inputs and return the dispatcher double."""
        constructed["session"] = session
        constructed["sender"] = sender
        constructed["clock"] = clock
        constructed["monotonic"] = monotonic
        return FakeDispatcher()

    settings = Settings(_env_file=None, telegram_bot_token="123:secret")
    sender = object()
    session = cast(AsyncSession, object())
    monkeypatch.setattr(delivery, "build_reminder_dispatcher", fake_build_dispatcher)
    monkeypatch.setattr(delivery, "TelegramBotSender", lambda _: sender)
    monkeypatch.setattr(delivery, "get_settings", lambda: settings)

    await dispatch_task_reminders(session=session)

    assert dispatched == [None]
    assert constructed["session"] is session
    assert constructed["sender"] is sender
    clock = cast(Callable[[], datetime], constructed["clock"])
    assert clock().tzinfo is UTC


def test_telegram_sender_requires_bot_token() -> None:
    """Constructing the sender without a token raises a configuration error."""
    settings = Settings(_env_file=None, telegram_bot_token=None)

    with pytest.raises(AuthNotConfiguredError):
        TelegramBotSender(settings)


async def test_telegram_sender_posts_and_returns_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sender posts form data to the Bot API and returns the message ID."""
    settings = Settings(_env_file=None, telegram_bot_token="123:secret")
    sender = TelegramBotSender(settings)
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
        """Capture the request and return a successful Telegram payload."""
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return FakeResponse(b'{"ok": true, "result": {"message_id": 42}}')

    monkeypatch.setattr(telegram_sender, "urlopen", fake_urlopen)

    message_id = await sender.send(7, "Reminder text")

    assert message_id == 42
    assert captured["url"] == "https://api.telegram.org/bot123:secret/sendMessage"
    assert captured["timeout"] is not None
    data = cast(bytes, captured["data"]).decode("utf-8")
    assert parse_qs(data) == {
        "chat_id": ["7"],
        "text": ["Reminder text"],
        "parse_mode": ["HTML"],
    }


async def test_telegram_sender_failure_message_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected request raises a generic error without the token or text."""
    settings = Settings(_env_file=None, telegram_bot_token="123:secret")
    sender = TelegramBotSender(settings)

    def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
        """Return a Telegram payload that rejects the message."""
        return FakeResponse(b'{"ok": false, "description": "Bad Request"}')

    monkeypatch.setattr(telegram_sender, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        await sender.send(7, "Secret reminder text")

    message = str(excinfo.value)
    assert "123:secret" not in message
    assert "Secret reminder text" not in message
    assert isinstance(excinfo.value.__cause__, type(None))


async def test_telegram_sender_network_failure_message_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL error raises a generic RuntimeError without token or text."""
    settings = Settings(_env_file=None, telegram_bot_token="123:secret")
    sender = TelegramBotSender(settings)

    def failing_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
        """Raise a network-level URL error before any HTTP response exists."""
        raise URLError("connection refused")

    monkeypatch.setattr(telegram_sender, "urlopen", failing_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        await sender.send(7, "Secret reminder text")

    message = str(excinfo.value)
    assert "123:secret" not in message
    assert "Secret reminder text" not in message
    assert isinstance(excinfo.value.__cause__, type(None))
    assert excinfo.value.__suppress_context__ is True
