from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.params import Depends
from httpx import ASGITransport, AsyncClient

from backend.api.deps import DatabaseProbeDep, SessionDep, get_session
from backend.core.config import Settings
from backend.db.base import Base
from backend.db.probe import DatabaseProbe
from backend.db.session import Database


class FakeSession:
    """Minimal async session double tracking transaction calls."""

    def __init__(self, commit_error: Exception | None = None) -> None:
        """Initialize the transaction and close flags."""
        self.commit_error = commit_error
        self.committed = False
        self.rolled_back = False
        self.closed = False

    async def commit(self) -> None:
        """Mark the fake transaction as committed and optionally fail."""
        self.committed = True
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        """Mark the fake transaction as rolled back."""
        self.rolled_back = True

    async def close(self) -> None:
        """Mark the fake session as closed."""
        self.closed = True

    async def __aenter__(self) -> FakeSession:
        """Return the fake session as its own context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the fake session when leaving the context."""
        await self.close()


class FakeSessionFactory:
    """Session factory double returning a prepared fake session."""

    def __init__(self, session: FakeSession) -> None:
        """Store the fake session returned from calls."""
        self.session = session

    def __call__(self) -> FakeSession:
        """Return the prepared fake session."""
        return self.session


class FakeDatabase:
    """Database double exposing the same commit/rollback session contract."""

    def __init__(self, session: FakeSession) -> None:
        """Store the fake session and initialize the call counter."""
        self.session_obj = session
        self.session_calls = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeSession]:
        """Yield the fake session, committing or rolling back around the block."""
        self.session_calls += 1
        async with self.session_obj as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


class FakeConnection:
    """Async connection double that optionally raises on execute."""

    def __init__(self, error: Exception | None = None) -> None:
        """Store the optional error raised on execute."""
        self.error = error

    async def execute(self, statement: object) -> None:
        """Raise the configured error or succeed silently."""
        if self.error is not None:
            raise self.error

    async def __aenter__(self) -> FakeConnection:
        """Return the fake connection as its own context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave the fake connection context without side effects."""
        return None


class FakeEngine:
    """Async engine double yielding fake connections."""

    def __init__(self, error: Exception | None = None) -> None:
        """Store the optional error for created connections."""
        self.error = error

    def connect(self) -> FakeConnection:
        """Return a fake connection configured with the stored error."""
        return FakeConnection(self.error)


@pytest.fixture
def settings() -> Settings:
    """Build settings without reading a local .env file."""
    return Settings(_env_file=None)


def test_naming_convention_present() -> None:
    """Expose the project naming convention on the declarative metadata."""
    convention = Base.metadata.naming_convention
    assert convention["ix"] == "ix_%(column_0_label)s"
    assert convention["uq"] == "uq_%(table_name)s_%(column_0_name)s"
    assert convention["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    assert convention["pk"] == "pk_%(table_name)s"


async def test_database_session_commits_on_success(settings: Settings) -> None:
    """Commit the transaction when the session block succeeds."""
    session = FakeSession()
    database = Database(settings)
    database.session_factory = FakeSessionFactory(session)  # type: ignore[assignment]

    async with database.session() as active:
        assert isinstance(active, FakeSession)
        assert active is session

    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


async def test_database_session_rolls_back_on_error(settings: Settings) -> None:
    """Roll back and re-raise when the session block fails."""
    session = FakeSession()
    database = Database(settings)
    database.session_factory = FakeSessionFactory(session)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="boom"):
        async with database.session():
            raise RuntimeError("boom")

    assert session.committed is False
    assert session.rolled_back is True
    assert session.closed is True


async def test_database_session_propagates_commit_failure(settings: Settings) -> None:
    """Roll back, close, and re-raise when the commit fails."""
    error = RuntimeError("commit failed")
    session = FakeSession(commit_error=error)
    database = Database(settings)
    database.session_factory = FakeSessionFactory(session)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="commit failed"):
        async with database.session():
            pass

    assert session.committed is True
    assert session.rolled_back is True
    assert session.closed is True


async def test_session_dependency_commits_before_response(app: FastAPI) -> None:
    """Commit and close the app-state session before returning a response."""
    session = FakeSession()
    database = FakeDatabase(session)
    app.state.db = database

    async def handler(active: SessionDep) -> dict[str, bool]:
        """Report whether the dependency yielded the fake session."""
        return {"fake": isinstance(active, FakeSession)}

    app.add_api_route("/_test/session", handler, methods=["GET"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/session")

    assert response.status_code == 200
    assert response.json() == {"fake": True}
    assert database.session_calls == 1
    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


async def test_session_dependency_commit_failure_returns_500(app: FastAPI) -> None:
    """Return an internal error when dependency cleanup fails to commit."""
    session = FakeSession(commit_error=RuntimeError("commit failed"))
    database = FakeDatabase(session)
    app.state.db = database

    async def handler(active: SessionDep) -> dict[str, str]:
        """Return a successful payload before dependency cleanup runs."""
        return {"status": "ok"}

    app.add_api_route("/_test/session", handler, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/session")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert database.session_calls == 1
    assert session.committed is True
    assert session.rolled_back is True
    assert session.closed is True


async def test_session_dependency_uses_function_scope() -> None:
    """Bind the session dependency to function-scoped cleanup."""
    dependency = get_args(SessionDep)[1]
    assert isinstance(dependency, Depends)
    assert dependency.dependency is get_session
    assert dependency.scope == "function"


async def test_probe_returns_true_when_database_responds() -> None:
    """Report readiness when a trivial query succeeds."""
    probe = DatabaseProbe(FakeEngine())  # type: ignore[arg-type]
    assert await probe.check() is True


async def test_probe_returns_false_when_database_fails() -> None:
    """Report unreadiness when the database query fails."""
    probe = DatabaseProbe(FakeEngine(RuntimeError("down")))  # type: ignore[arg-type]
    assert await probe.check() is False


def test_dependency_aliases_are_annotated() -> None:
    """Expose annotated aliases for the request-scoped dependencies."""
    assert get_args(DatabaseProbeDep)
    assert get_args(SessionDep)
