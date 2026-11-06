from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from nats.js.api import ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from taskiq import AckableMessage, AsyncBroker, AsyncResultBackend, TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.serializers import JSONSerializer
from taskiq_nats import PullBasedJetStreamBroker
from taskiq_redis import RedisAsyncResultBackend

from backend.core.config import Settings, get_settings
from backend.tasks.middleware import TaskLoggingMiddleware


class ResilientPullBasedJetStreamBroker(PullBasedJetStreamBroker):
    """Pull-based JetStream broker that survives fetch timeout races."""

    async def listen(self) -> AsyncGenerator[AckableMessage]:
        """
        Yield messages while ignoring pull-fetch timeouts.

        Purpose:
            Keep the worker alive when nats-py raises a built-in
            ``asyncio.TimeoutError`` from a pull fetch instead of the
            ``nats.errors.TimeoutError`` that taskiq-nats catches.

        Returns:
            AsyncGenerator[AckableMessage, None]: Messages delivered by the
                underlying pull subscription.

        Notes:
            - ``nats.errors.TimeoutError`` and ``asyncio.TimeoutError`` both
              inherit from the built-in ``TimeoutError``.
            - Cancellation is not a ``TimeoutError`` and still stops the
              generator.
        """
        while True:
            try:
                async for message in super().listen():
                    yield message
            except TimeoutError:
                continue


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
        - Pull-fetch timeout races are absorbed by
          ``ResilientPullBasedJetStreamBroker`` so the worker listener keeps
          running.
    """
    result_backend: AsyncResultBackend[Any] = RedisAsyncResultBackend(
        redis_url=settings.redis_url,
        result_ex_time=settings.taskiq_result_ex_time,
        keep_results=settings.taskiq_keep_results,
        serializer=JSONSerializer(),
    )
    task_broker: AsyncBroker = (
        ResilientPullBasedJetStreamBroker(
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
