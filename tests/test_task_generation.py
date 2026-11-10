from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.tasks import generation
from backend.tasks.generation import generate_task_series

_FIXED_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class FixedDateTime(datetime):
    """Datetime subclass returning a deterministic current instant."""

    @classmethod
    def now(cls, tz: Any = None) -> FixedDateTime:
        """Return the fixed current instant, converted when a timezone is given."""
        fixed = _FIXED_NOW.replace(tzinfo=None) if tz is None else _FIXED_NOW.astimezone(tz)
        return cls(
            fixed.year,
            fixed.month,
            fixed.day,
            fixed.hour,
            fixed.minute,
            fixed.second,
            fixed.microsecond,
            tzinfo=fixed.tzinfo,
        )


def test_generation_task_has_schedule() -> None:
    """The generation task carries its fifteen-minute cron label."""
    schedule = generate_task_series.labels["schedule"]
    assert schedule[0]["cron"] == "*/15 * * * *"
    assert schedule[0]["schedule_id"] == "task-series-generation"


async def test_generation_task_delegates_to_series_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task body calls the series service with the current UTC time."""
    calls: list[datetime] = []

    class FakeSeriesService:
        """Series service double recording the generation instant."""

        async def generate(self, now: datetime) -> int:
            """Record the generation instant and return a fixed count."""
            calls.append(now)
            return 3

    def fake_build_series_service(session: AsyncSession) -> FakeSeriesService:
        """Return the series service double for the supplied job session."""
        constructed["session"] = session
        return FakeSeriesService()

    constructed: dict[str, object] = {}
    monkeypatch.setattr(generation, "build_series_service", fake_build_series_service)
    monkeypatch.setattr(generation, "datetime", FixedDateTime)

    session = cast(AsyncSession, object())
    await generate_task_series(session=session)

    assert calls == [_FIXED_NOW]
    assert constructed["session"] is session
