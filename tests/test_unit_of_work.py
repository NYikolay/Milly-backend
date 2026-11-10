"""Tests for the SQLAlchemy unit-of-work adapter."""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from backend.infrastructure.persistence.sqlalchemy.unit_of_work import (
    SqlAlchemyUnitOfWork,
)


class RecordingSession:
    """Minimal session double recording transaction calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def flush(self) -> None:
        """Record a flush call."""
        self.calls.append("flush")

    async def commit(self) -> None:
        """Record a commit call."""
        self.calls.append("commit")

    async def rollback(self) -> None:
        """Record a rollback call."""
        self.calls.append("rollback")


async def test_unit_of_work_delegates_flush_commit_rollback() -> None:
    """The adapter delegates transaction calls to the session."""
    session = RecordingSession()
    uow = SqlAlchemyUnitOfWork(cast(AsyncSession, session))
    await uow.flush()
    await uow.commit()
    await uow.rollback()
    assert session.calls == ["flush", "commit", "rollback"]
