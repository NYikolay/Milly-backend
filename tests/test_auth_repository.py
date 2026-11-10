from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.auth.models import TelegramIdentity, TelegramUser
from backend.domain.entities import AuthSession as AuthSessionEntity
from backend.domain.entities import User as UserEntity
from backend.infrastructure.persistence.sqlalchemy.auth_repository import (
    SqlAlchemyAuthRepository,
)
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    auth_session_to_entity,
    profile_to_entity,
)
from backend.models.auth_session import AuthSession as AuthSessionRow
from backend.models.telegram_profile import TelegramProfile as TelegramProfileRow
from backend.models.user import User as UserRow

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
USER_ID = 7
PROFILE_ID = 9
TELEGRAM_ID = 123
SESSION_ID = 70
NEW_USER_ID = 42
NEW_PROFILE_ID = 43
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

    def __init__(self, *, scalar: Any = None, row: Any = None) -> None:
        """
        Configure the values returned by each result accessor.

        Purpose:
            Let each repository test provide only the result shape it reads.

        Args:
            scalar: Value returned by ``scalar_one_or_none``.
            row: Value returned by ``one_or_none``.
        """
        self._scalar = scalar
        self._row = row

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
            Support lookup repository methods.

        Returns:
            Any: Configured scalar value or ``None``.
        """
        return self._scalar

    def one_or_none(self) -> Any:
        """
        Return the configured row tuple.

        Purpose:
            Support joined user/profile lookups.

        Returns:
            Any: Configured row tuple or ``None``.
        """
        return self._row


class RecordingSession:
    """Async session double that records statements and write operations."""

    def __init__(
        self,
        results: Sequence[RecordingResult] | None = None,
        *,
        get_results: Sequence[Any] | None = None,
        assigned_ids: Sequence[int] | None = None,
    ) -> None:
        """
        Initialize the recording session.

        Purpose:
            Capture SQLAlchemy statements and return prepared results in call
            order.

        Args:
            results: Results to return from successive ``execute`` calls.
            get_results: ORM rows to return from successive primary-key
                lookups.
            assigned_ids: Identifiers assigned to newly added rows during
                flushes, simulating PostgreSQL identity columns.
        """
        self.statements: list[Any] = []
        self.parameters: list[Any] = []
        self.added: list[Any] = []
        self.flush_count = 0
        self.commit_count = 0
        self._results = list(results or [])
        self._get_results = list(get_results or [])
        self._assigned_ids = list(assigned_ids or [])

    async def execute(self, statement: Any, params: Any = None) -> RecordingResult:
        """
        Record a statement and return the next prepared result.

        Purpose:
            Let repository tests inspect exactly what would be sent to
            PostgreSQL.

        Args:
            statement: SQLAlchemy statement passed by the repository.
            params: Optional bound parameters passed with the statement.

        Returns:
            RecordingResult: The next prepared result or an empty default.
        """
        self.statements.append(statement)
        self.parameters.append(params)
        if self._results:
            return self._results.pop(0)
        return RecordingResult()

    async def get(self, entity: Any, ident: Any) -> Any:
        """
        Return the next prepared ORM row for a primary-key lookup.

        Purpose:
            Support update methods that resolve the tracked row before
            applying domain values.

        Args:
            entity: Mapped class requested by the repository.
            ident: Primary-key value requested by the repository.

        Returns:
            Any: The next prepared row, or ``None``.
        """
        if self._get_results:
            return self._get_results.pop(0)
        return None

    def add(self, instance: Any) -> None:
        """
        Record an object added to the session.

        Purpose:
            Verify insert methods add their entity before flushing.

        Args:
            instance: ORM object passed to ``add``.
        """
        self.added.append(instance)

    async def flush(self) -> None:
        """
        Record a flush and assign identities to new rows.

        Purpose:
            Verify write methods flush without committing and simulate the
            identifiers PostgreSQL generates for inserted rows.

        Returns:
            None: No database work is performed.
        """
        self.flush_count += 1
        for instance in self.added:
            if getattr(instance, "id", None) is None and self._assigned_ids:
                instance.id = self._assigned_ids.pop(0)

    async def commit(self) -> None:
        """
        Record an explicit commit.

        Purpose:
            Verify the adapter never commits on its own.

        Returns:
            None: No database work is performed.
        """
        self.commit_count += 1


def make_user_row(**overrides: Any) -> UserRow:
    """Build an ORM user row with deterministic defaults."""
    values: dict[str, Any] = {
        "id": USER_ID,
        "timezone": "UTC",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return UserRow(**values)


def make_profile_row(**overrides: Any) -> TelegramProfileRow:
    """Build an ORM Telegram profile row with deterministic defaults."""
    values: dict[str, Any] = {
        "id": PROFILE_ID,
        "user_id": USER_ID,
        "telegram_id": TELEGRAM_ID,
        "is_bot": False,
        "first_name": "Ivan",
        "last_name": None,
        "username": None,
        "language_code": None,
        "is_premium": False,
        "added_to_attachment_menu": None,
        "allows_write_to_pm": None,
        "photo_url": None,
    }
    values.update(overrides)
    return TelegramProfileRow(**values)


def make_session_row(**overrides: Any) -> AuthSessionRow:
    """Build an ORM auth-session row with deterministic defaults."""
    values: dict[str, Any] = {
        "id": SESSION_ID,
        "user_id": USER_ID,
        "family_id": FAMILY_ID,
        "refresh_token_hash": TOKEN_HASH,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=1),
        "revoked_at": None,
    }
    values.update(overrides)
    return AuthSessionRow(**values)


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


async def test_auth_repository_returns_domain_identity() -> None:
    """Profile lookups return the domain user/profile pair, not ORM rows."""
    user_row = make_user_row()
    profile_row = make_profile_row()
    session = RecordingSession(results=[RecordingResult(row=(user_row, profile_row))])
    repository = make_repository(session)

    identity = await repository.get_identity_by_telegram_id(TELEGRAM_ID)
    sql = compile_statement(session.statements[-1])

    assert isinstance(identity, TelegramIdentity)
    assert identity.profile.telegram_id == TELEGRAM_ID
    assert identity.user.id == identity.profile.user_id
    assert identity.user.telegram_profile is identity.profile
    assert "JOIN telegram_profiles" in sql
    assert "telegram_profiles.telegram_id = 123" in sql
    assert len(session.statements) == 1
    assert session.commit_count == 0


async def test_auth_repository_returns_none_for_unknown_identity() -> None:
    """An unknown Telegram id produces no domain identity."""
    session = RecordingSession(results=[RecordingResult(row=None)])
    repository = make_repository(session)

    identity = await repository.get_identity_by_telegram_id(TELEGRAM_ID)

    assert identity is None
    assert len(session.statements) == 1
    assert session.commit_count == 0


async def test_create_user_with_profile_returns_generated_identity() -> None:
    """New identities are flushed twice and expose generated user/profile ids."""
    session = RecordingSession(assigned_ids=[NEW_USER_ID, NEW_PROFILE_ID])
    repository = make_repository(session)
    data = TelegramUser(
        id=TELEGRAM_ID,
        first_name="Ivan",
        last_name="Petrov",
        username="ivan",
        language_code="ru",
        is_premium=True,
        is_bot=False,
        added_to_attachment_menu=True,
        allows_write_to_pm=True,
        photo_url="https://example.com/photo.png",
    )

    identity = await repository.create_user_with_profile(data)

    user_row = cast(UserRow, session.added[0])
    profile_row = cast(TelegramProfileRow, session.added[1])
    assert isinstance(identity, TelegramIdentity)
    assert identity.user.id == NEW_USER_ID
    assert identity.profile.id == NEW_PROFILE_ID
    assert identity.profile.user_id == NEW_USER_ID
    assert identity.user.telegram_profile is identity.profile
    assert user_row.id == NEW_USER_ID
    assert profile_row.id == NEW_PROFILE_ID
    assert profile_row.user_id == NEW_USER_ID
    assert profile_row.telegram_id == TELEGRAM_ID
    assert profile_row.first_name == "Ivan"
    assert profile_row.is_premium is True
    assert session.flush_count == 2
    assert session.commit_count == 0


async def test_update_profile_updates_mutable_fields_and_flushes() -> None:
    """Profile updates refresh mutable fields and preserve identity columns."""
    profile_row = make_profile_row(first_name="Old", username="old")
    profile = profile_to_entity(profile_row)
    session = RecordingSession(get_results=[profile_row])
    repository = make_repository(session)
    data = TelegramUser(
        id=TELEGRAM_ID,
        first_name="New",
        last_name="Petrov",
        username="new",
        language_code="ru",
        is_premium=True,
        is_bot=True,
        added_to_attachment_menu=False,
        allows_write_to_pm=True,
        photo_url="https://example.com/photo.png",
    )

    await repository.update_profile(profile, data)

    assert profile_row.first_name == "New"
    assert profile_row.last_name == "Petrov"
    assert profile_row.username == "new"
    assert profile_row.language_code == "ru"
    assert profile_row.is_premium is True
    assert profile_row.added_to_attachment_menu is False
    assert profile_row.allows_write_to_pm is True
    assert profile_row.photo_url == "https://example.com/photo.png"
    assert profile_row.id == PROFILE_ID
    assert profile_row.user_id == USER_ID
    assert profile_row.telegram_id == TELEGRAM_ID
    assert profile_row.is_bot is False
    assert profile.first_name == "New"
    assert profile.username == "new"
    assert session.flush_count == 1
    assert session.statements == []
    assert session.commit_count == 0


async def test_update_profile_requires_a_tracked_profile() -> None:
    """Updating a missing profile raises an invariant error."""
    session = RecordingSession()
    repository = make_repository(session)
    profile = profile_to_entity(make_profile_row())

    with pytest.raises(LookupError):
        await repository.update_profile(
            profile,
            TelegramUser(id=TELEGRAM_ID, first_name="New"),
        )

    assert session.flush_count == 0
    assert session.commit_count == 0


async def test_get_session_for_update_compiles_for_update() -> None:
    """Session lookup locks the row matching the hashed refresh token."""
    session_row = make_session_row()
    session = RecordingSession(results=[RecordingResult(scalar=session_row)])
    repository = make_repository(session)

    result = await repository.get_session_for_update(TOKEN_HASH)
    sql = compile_statement(session.statements[-1])

    assert isinstance(result, AuthSessionEntity)
    assert result.id == SESSION_ID
    assert result.user_id == USER_ID
    assert result.family_id == FAMILY_ID
    assert f"auth_sessions.refresh_token_hash = '{TOKEN_HASH}'" in sql
    assert "FOR UPDATE" in sql
    assert session.commit_count == 0


async def test_get_telegram_id_for_session_returns_scalar() -> None:
    """Session-based identity lookup returns only the Telegram identifier."""
    session = RecordingSession(results=[RecordingResult(scalar=TELEGRAM_ID)])
    repository = make_repository(session)

    telegram_id = await repository.get_telegram_id_for_session(TOKEN_HASH)
    sql = compile_statement(session.statements[-1])

    assert telegram_id == TELEGRAM_ID
    assert "JOIN auth_sessions" in sql
    assert f"auth_sessions.refresh_token_hash = '{TOKEN_HASH}'" in sql
    assert session.commit_count == 0


async def test_create_session_returns_domain_session() -> None:
    """New sessions are flushed once and mapped back to a domain entity."""
    session = RecordingSession(assigned_ids=[SESSION_ID])
    repository = make_repository(session)
    expires_at = NOW + timedelta(days=1)

    result = await repository.create_session(
        user_id=USER_ID,
        family_id=FAMILY_ID,
        refresh_token_hash=TOKEN_HASH,
        expires_at=expires_at,
    )

    row = cast(AuthSessionRow, session.added[0])
    assert isinstance(result, AuthSessionEntity)
    assert result.id == SESSION_ID
    assert result.user_id == USER_ID
    assert result.family_id == FAMILY_ID
    assert result.refresh_token_hash == TOKEN_HASH
    assert result.expires_at == expires_at
    assert row.id == SESSION_ID
    assert session.flush_count == 1
    assert session.commit_count == 0


async def test_revoke_session_marks_only_the_selected_row() -> None:
    """Single-session revocation targets the primary key and flushes."""
    auth_session = auth_session_to_entity(make_session_row())
    session = RecordingSession()
    repository = make_repository(session)

    await repository.revoke_session(auth_session, NOW)
    sql = compile_statement(session.statements[-1])

    assert sql.startswith("UPDATE auth_sessions SET revoked_at=")
    assert "auth_sessions.id = 70" in sql
    assert auth_session.revoked_at == NOW
    assert session.flush_count == 1
    assert session.commit_count == 0


async def test_revoke_user_sessions_filters_by_user_and_active_rows() -> None:
    """User-wide revocation only touches non-revoked sessions of that owner."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.revoke_user_sessions(USER_ID, NOW)
    sql = compile_statement(session.statements[-1])

    assert sql.startswith("UPDATE auth_sessions SET revoked_at=")
    assert "auth_sessions.user_id = 7" in sql
    assert "auth_sessions.revoked_at IS NULL" in sql
    assert session.commit_count == 0


async def test_revoke_family_updates_only_active_rows() -> None:
    """Family revocation only touches non-revoked sessions of that family."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.revoke_family(FAMILY_ID, NOW)
    sql = compile_statement(session.statements[-1])

    assert sql.startswith("UPDATE auth_sessions SET revoked_at=")
    assert "auth_sessions.family_id =" in sql
    assert str(FAMILY_ID) in sql
    assert "auth_sessions.revoked_at IS NULL" in sql
    assert session.commit_count == 0


async def test_get_user_with_profile_returns_domain_user() -> None:
    """User lookups eagerly map both the user and its Telegram profile."""
    user_row = make_user_row(timezone="Europe/Moscow")
    user_row.telegram_profile = make_profile_row()
    session = RecordingSession(results=[RecordingResult(scalar=user_row)])
    repository = make_repository(session)

    user = await repository.get_user_with_profile(USER_ID)
    sql = compile_statement(session.statements[-1])

    assert isinstance(user, UserEntity)
    assert user.id == USER_ID
    assert user.timezone == "Europe/Moscow"
    assert user.telegram_profile is not None
    assert user.telegram_profile.telegram_id == TELEGRAM_ID
    assert "users.id = 7" in sql
    assert session.commit_count == 0


async def test_update_user_timezone_returns_domain_user() -> None:
    """Timezone updates execute the update then map the refreshed user."""
    user_row = make_user_row(timezone="Europe/Moscow")
    user_row.telegram_profile = make_profile_row()
    session = RecordingSession(
        results=[
            RecordingResult(),
            RecordingResult(scalar=user_row),
        ]
    )
    repository = make_repository(session)

    user = await repository.update_user_timezone(USER_ID, "Europe/Moscow")
    update_sql = compile_statement(session.statements[0])
    select_sql = compile_statement(session.statements[1])

    assert isinstance(user, UserEntity)
    assert user.timezone == "Europe/Moscow"
    assert user.telegram_profile is not None
    assert "UPDATE users SET timezone='Europe/Moscow'" in update_sql
    assert "users.id = 7" in update_sql
    assert select_sql.startswith("SELECT")
    assert "users.id = 7" in select_sql
    assert session.commit_count == 0


async def test_acquire_telegram_lock_uses_advisory_lock() -> None:
    """Login serialization uses the transaction-scoped advisory lock."""
    session = RecordingSession()
    repository = make_repository(session)

    await repository.acquire_telegram_lock(TELEGRAM_ID)
    sql = str(session.statements[-1])

    assert "pg_advisory_xact_lock" in sql
    assert session.parameters[-1] == {"telegram_id": TELEGRAM_ID}
    assert session.commit_count == 0


def test_sqlalchemy_auth_repository_exposes_no_commit_helpers() -> None:
    """The adapter exposes only flush-based revocation methods."""
    repository = make_repository(RecordingSession())

    assert not hasattr(repository, "revoke_session_and_commit")
    assert not hasattr(repository, "revoke_family_and_commit")
    assert not hasattr(repository, "commit")
    assert not hasattr(repository, "rollback")
