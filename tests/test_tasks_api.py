"""Tests for the task enqueue API endpoint."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from taskiq.exceptions import SendTaskError

from backend.tasks.example import ping


class FakeTask:
    """Minimal enqueued task stand-in exposing a task ID."""

    task_id = "task-1"


async def test_enqueue_ping(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful enqueue returns 202 with the task ID."""

    async def fake_kiq() -> FakeTask:
        return FakeTask()

    monkeypatch.setattr(ping, "kiq", fake_kiq)
    response = await client.post("/api/v1/tasks/ping")
    assert response.status_code == 202
    assert response.json() == {"task_id": "task-1"}


async def test_enqueue_ping_failure_returns_503(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker failure is rendered as the unified task enqueue error."""

    async def failing_kiq() -> FakeTask:
        raise SendTaskError()

    monkeypatch.setattr(ping, "kiq", failing_kiq)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/tasks/ping")
    assert response.status_code == 503
    assert response.json()["code"] == "task_enqueue_failed"
