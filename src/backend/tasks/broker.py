from __future__ import annotations

from typing import Any

from nats.js.api import ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from taskiq import AsyncBroker, AsyncResultBackend, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.serializers import JSONSerializer
from taskiq_nats import PullBasedJetStreamBroker
from taskiq_redis import RedisAsyncResultBackend

from backend.core.config import Settings, get_settings
from backend.tasks.middleware import TaskLoggingMiddleware


def create_broker(settings: Settings) -> AsyncBroker:
    """
    Create a NATS JetStream broker with a Redis result backend.

    Purpose:
        Builds the configured broker used by the worker and task producers
        without opening any network connections.

    Args:
        settings: Application settings with NATS and Redis connection values.

    Returns:
        AsyncBroker: Broker with the Redis result backend and logging middleware.

    Notes:
        - Constructing the broker is lazy; connections are opened on startup.
    """
    result_backend: AsyncResultBackend[Any] = RedisAsyncResultBackend(
        redis_url=settings.redis_url,
        result_ex_time=settings.taskiq_result_ex_time,
        keep_results=settings.taskiq_keep_results,
        serializer=JSONSerializer(),
    )
    task_broker: AsyncBroker = (
        PullBasedJetStreamBroker(
            servers=settings.nats_url,
            subject=settings.nats_subject,
            stream_name=settings.nats_stream,
            durable=settings.nats_durable,
            user=settings.nats_user,
            password=settings.nats_password,
            stream_config=StreamConfig(
                name=settings.nats_stream,
                subjects=[settings.nats_subject],
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
                max_age=settings.nats_stream_max_age,
            ),
            consumer_config=ConsumerConfig(
                durable_name=settings.nats_durable,
                max_deliver=settings.nats_max_deliver,
                ack_wait=settings.nats_ack_wait,
            ),
            pull_consume_batch=10,
            pull_consume_timeout=5.0,
        )
        .with_result_backend(result_backend)
        .with_middlewares(TaskLoggingMiddleware())
    )
    return task_broker


settings = get_settings()
broker = create_broker(settings)
scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)
