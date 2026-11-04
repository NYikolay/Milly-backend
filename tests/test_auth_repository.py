from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.repository import SqlAlchemyAuthRepository
from backend.models.auth_session import AuthSession

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
USER_ID = 7
TOKEN_HASH = "a" * 64
FAMILY_ID = UUID("11111111-1111-1111-1111-111111111111")


class RecordingScalars:
    """Scalar collection returned by the recording result."""

    def __init__(self, values: Sequence[Any]) -> None:
        """
        Store the scalar values to return.

        Purpose:
            Give repository callers an iterable result without a database.

        Args:
            values: Values returned by ``all()``.
        """
        self._values = list(values)

    def all(self) -> list[Any]:
        """
        Return every recorded scalar value.

        Purpose:
            Match the SQLAlchemy ``ScalarResult`` contract used by the
            repository.

        Returns:
            list[Any]: Stored scalar values.
        """
        return self._values


class RecordingResult:
    """Configurable result returned by the recording session."""

    def __init__(self, *, scalar: Any = None) -> None:
        """
        Configure the value returned by optional scalar lookups.

        Purpose:
            Let each repository test provide only the result shape it reads.

        Args:
            scalar: Value returned by ``scalar_one_or_none``.
        """
        self._scalar = scalar

    def scalars(self) -> RecordingScalars:
        """
        Return the scalar collection.

        Purpose:
            Support list-returning repository methods.

        Returns:
            RecordingScalars: Empty scalar values.
        """
        return RecordingScalars(())

    def scalar_one_or_none(self) -> Any:
        """
        Return the configured optional scalar value.

        Purpose:
            Support lookup and existence repository methods.

        Returns:
            Any: Configured scalar value or ``None``.
        """
        return self._scalar


class RecordingSession:
    """Async session double that records statements and commits."""

    def __init__(self, results: Sequence[RecordingResult] | None = None) -> None:
        """
        Initialize the recording session.

        Purpose:
            Capture SQLAlchemy statements and return prepared results in call
            order.

        Args:
            results: Results to return from successive ``execute`` calls.
        """
        self.statements: list[Any] = []
        self.commit_count = 0
        self._results = list(results or [])

    async def execute(self, statement: Any) -> RecordingResult:
        """
        Record a statement and return the next prepared result.

        Purpose:
            Let repository tests inspect exactly what would be sent to
            PostgreSQL.

        Args:
            statement: SQLAlchemy statement passed by the repository.

        Returns:
            RecordingResult: The next prepared result or an empty default.
        """
        self.statements.append(statement)
        if self._results:
            return self._results.pop(0)
        return RecordingResult()

    async def commit(self) -> None:
        """
        Record an explicit commit.

        Purpose:
            Verify revoke helpers persist before propagating their error path.

        Returns:
            None: No database work is performed.
        """
        self.commit_count += 1


def make_repository(session: RecordingSession) -> SqlAlchemyAuthRepository:
    """Build an auth repository bound to the recording session."""
    return SqlAlchemyAuthRepository(cast(AsyncSession, session))


def compile_statement(statement: Any) -> str:
    """Compile one recorded statement with literal binds for PostgreSQL."""
    return str(
        statement.compile(
            dialect=cast(Any, postgresql.dialect)(),
            compile_kwargs={"literal_binds": True},
        )
    )


async def test_get_session_for_update_locks_by_token_hash() -> None:
    """Session lookup locks the row matching the hashed refresh token."""
    session = RecordingSession(results=[RecordingResult(scalar=None)])
    repository = make_repository(session)

    result = await repository.get_session_for_update(TOKEN_HASH)
    sql = compile_statement(session.statements[-1])

    assert result is None
    assert f"auth_sessions.refresh_token_hash = '{TOKEN_HASH}'" in sql
    assert "FOR UPDATE" in sql


async def test_revoke_user_sessions_filters_by_user_and_active_rows() -> None:
    """User-wide revocation only touches non-revoked sessions of that owner."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.revoke_user_sessions(USER_ID, NOW)
    sql = compile_statement(session.statements[-1])

    assert sql.startswith("UPDATE auth_sessions SET revoked_at=")
    assert "auth_sessions.user_id = 7" in sql
    assert "auth_sessions.revoked_at IS NULL" in sql


async def test_revoke_family_filters_by_family_and_active_rows() -> None:
    """Family revocation only touches non-revoked sessions of that family."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.revoke_family(FAMILY_ID, NOW)
    sql = compile_statement(session.statements[-1])

    assert sql.startswith("UPDATE auth_sessions SET revoked_at=")
    assert "auth_sessions.family_id =" in sql
    assert str(FAMILY_ID) in sql
    assert "auth_sessions.revoked_at IS NULL" in sql


async def test_revoke_session_and_commit_marks_then_commits() -> None:
    """Session revocation sets the timestamp and commits it immediately."""
    session = RecordingSession()
    repository = make_repository(session)
    auth_session = AuthSession(
        id=1,
        user_id=USER_ID,
        family_id=FAMILY_ID,
        refresh_token_hash=TOKEN_HASH,
        expires_at=NOW,
    )

    await repository.revoke_session_and_commit(auth_session, NOW)

    assert auth_session.revoked_at == NOW
    assert session.commit_count == 1
    assert session.statements == []


async def test_revoke_family_and_commit_updates_then_commits() -> None:
    """Family revocation executes the update and commits it immediately."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.revoke_family_and_commit(FAMILY_ID, NOW)
    sql = compile_statement(session.statements[-1])

    assert "auth_sessions.family_id =" in sql
    assert str(FAMILY_ID) in sql
    assert session.commit_count == 1
