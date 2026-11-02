from typing import cast

from taskiq_nats import PullBasedJetStreamBroker
from taskiq_redis import RedisAsyncResultBackend

from backend.core.config import Settings
from backend.tasks.broker import broker, create_broker
from backend.tasks.example import heartbeat, ping
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


def test_heartbeat_has_schedule_label() -> None:
    """The heartbeat task carries its cron schedule label."""
    schedule = heartbeat.labels["schedule"]
    assert schedule[0]["cron"] == "*/5 * * * *"
    assert schedule[0]["schedule_id"] == "heartbeat"


def test_ping_and_heartbeat_are_registered() -> None:
    """Example tasks are registered under their expected names."""
    assert ping.task_name == "ping"
    assert heartbeat.task_name == "heartbeat"
