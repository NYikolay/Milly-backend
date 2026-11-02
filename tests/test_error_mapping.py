from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import (
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError,
)
from taskiq.exceptions import SendTaskError

from backend.api.errors.mapping import (
    payload_from_sqlalchemy_error,
    payload_from_taskiq_error,
)
from backend.core.errors import ErrorCode, get_error_spec


@asynccontextmanager
async def _error_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield a client that returns server errors instead of re-raising them."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_operational_error_maps_to_database_unavailable(app: FastAPI) -> None:
    """Map a database operational failure to a 503 database error envelope."""

    async def raise_operational() -> None:
        """Raise a database operational error."""
        raise OperationalError("SELECT 1", {}, Exception("down"))

    app.add_api_route("/_test/operational", raise_operational, methods=["GET"])
    async with _error_client(app) as client:
        response = await client.get("/_test/operational")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "database_unavailable"
    assert body["message"] == "Database is unavailable"
    assert body["description"] == "База данных временно недоступна"
    assert "details" not in body


async def test_generic_sqlalchemy_error_maps_to_internal_error(app: FastAPI) -> None:
    """Map a non-connectivity SQLAlchemy failure to an internal error envelope."""

    async def raise_sqlalchemy() -> None:
        """Raise a generic SQLAlchemy error."""
        raise SQLAlchemyError("boom")

    app.add_api_route("/_test/sqlalchemy", raise_sqlalchemy, methods=["GET"])
    async with _error_client(app) as client:
        response = await client.get("/_test/sqlalchemy")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "details" not in body


async def test_integrity_error_stays_internal_error(app: FastAPI) -> None:
    """Keep integrity errors internal because business conflicts must be explicit."""

    async def raise_integrity() -> None:
        """Raise a database integrity error."""
        raise IntegrityError("stmt", {}, Exception("duplicate"))

    app.add_api_route("/_test/integrity", raise_integrity, methods=["GET"])
    async with _error_client(app) as client:
        response = await client.get("/_test/integrity")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert "details" not in body


async def test_taskiq_error_maps_to_task_enqueue_failed(app: FastAPI) -> None:
    """Map a task broker failure to a 503 task enqueue error envelope."""

    async def raise_taskiq() -> None:
        """Raise a task broker send error."""
        raise SendTaskError()

    app.add_api_route("/_test/taskiq", raise_taskiq, methods=["GET"])
    async with _error_client(app) as client:
        response = await client.get("/_test/taskiq")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "task_enqueue_failed"
    assert body["message"] == "Failed to enqueue task"
    assert body["description"] == "Не удалось поставить задачу в очередь"
    assert "details" not in body


@pytest.mark.parametrize(
    "exc",
    [
        OperationalError("SELECT 1", {}, Exception("down")),
        InterfaceError("SELECT 1", {}, Exception("down")),
        DisconnectionError("down"),
        TimeoutError("timeout"),
    ],
)
def test_connectivity_errors_map_to_database_unavailable(exc: SQLAlchemyError) -> None:
    """Map every database connectivity failure to the database error spec."""
    payload = payload_from_sqlalchemy_error(exc)
    spec = get_error_spec(ErrorCode.DATABASE_UNAVAILABLE)
    assert payload.status_code == spec.status_code
    assert payload.code == ErrorCode.DATABASE_UNAVAILABLE
    assert payload.log_level == spec.log_level
    assert payload.details is None


def test_taskiq_payload_uses_registry_spec() -> None:
    """Build the task enqueue payload from the registry without details."""
    payload = payload_from_taskiq_error(SendTaskError())
    spec = get_error_spec(ErrorCode.TASK_ENQUEUE_FAILED)
    assert payload.status_code == spec.status_code
    assert payload.code == ErrorCode.TASK_ENQUEUE_FAILED
    assert payload.log_level == spec.log_level
    assert payload.details is None
