from typing import Any, cast

from taskiq_nats import PullBasedJetStreamBroker
from taskiq_redis import RedisAsyncResultBackend

from backend.core.config import Settings
from backend.tasks.broker import broker, create_broker
from backend.tasks.middleware import TaskLoggingMiddleware


def test_default_broker_uses_redis_result_backend() -> None:
    """The module-level broker stores results in Redis."""
    assert isinstance(broker.result_backend, RedisAsyncResultBackend)


def test_create_broker_uses_settings() -> None:
    """A broker built from custom settings keeps the Redis result backend."""
    settings = Settings(
        _env_file=None,
        nats_url="nats://example:4222",
        nats_stream="CUSTOM",
        nats_subject="custom.tasks",
        redis_url="redis://example:6379/1",
    )
    custom = create_broker(settings)
    assert isinstance(custom.result_backend, RedisAsyncResultBackend)


def test_task_logging_middleware_is_attached() -> None:
    """The default broker includes the task logging middleware."""
    assert any(isinstance(item, TaskLoggingMiddleware) for item in broker.middlewares)


class _FakeNatsMessage:
    """NATS message double carrying one payload."""

    data = b"payload"

    async def ack(self) -> None:
        """Acknowledge the message."""


class _TimeoutThenMessageConsumer:
    """Pull consumer double raising a built-in timeout before one message."""

    def __init__(self) -> None:
        """Initialize the fetch counter."""
        self.fetch_calls = 0

    async def fetch(self, **kwargs: Any) -> list[_FakeNatsMessage]:
        """Raise ``asyncio.TimeoutError`` once, then return a message."""
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            raise TimeoutError
        return [_FakeNatsMessage()]


async def test_broker_survives_fetch_timeout_race() -> None:
    """Keep listening when nats-py raises a built-in fetch timeout."""
    settings = Settings(_env_file=None, nats_url="nats://example:4222")
    custom = cast(PullBasedJetStreamBroker, create_broker(settings))
    consumer = _TimeoutThenMessageConsumer()
    cast(Any, custom).consumer = consumer

    iterator = custom.listen()
    try:
        message = await anext(iterator)
    finally:
        await iterator.aclose()

    assert consumer.fetch_calls == 2
    assert message.data == b"payload"


def test_create_broker_uses_consumer_and_stream_settings() -> None:
    """Custom NATS settings drive consumer timing and stream configuration."""
    settings = Settings(
        _env_file=None,
        nats_stream="CUSTOM",
        nats_subject="custom.tasks",
        nats_ack_wait=321,
        nats_max_deliver=4,
    )
    custom = cast(PullBasedJetStreamBroker, create_broker(settings))
    assert custom.consumer_config is not None
    assert custom.consumer_config.ack_wait == 321
    assert not custom.consumer_config.backoff
    assert custom.consumer_config.max_deliver == 4
    assert custom.stream_config.name == "CUSTOM"
    assert custom.stream_config.subjects == ["custom.tasks"]
