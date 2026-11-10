from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from backend.application.auth.errors import (
    AuthNotConfiguredError,
    RefreshTokenGraceError,
)
from backend.application.auth.service import AuthService
from backend.core.errors import BadRequestError, ErrorCode, UnauthorizedError
from backend.domain.auth.models import AuthSettings, TelegramIdentity, TelegramUser
from backend.domain.auth.tokens import hash_refresh_token
from backend.domain.entities import AuthSession as AuthSessionEntity
from backend.domain.entities import TelegramProfile, User

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SECRET = "a" * 32
BOT_TOKEN = "test-bot-token"
SESSION_ID = 77
FAMILY_ID = UUID("00000000-0000-0000-0000-000000000001")


def clock() -> datetime:
    """Return the fixed test clock."""
    return NOW


def make_auth_settings(**overrides: Any) -> AuthSettings:
    """Build plain auth settings with deterministic values."""
    values: dict[str, Any] = {
        "telegram_bot_token": BOT_TOKEN,
        "auth_jwt_secret": SECRET,
        "telegram_init_data_max_age_seconds": 3600,
    }
    values.update(overrides)
    return AuthSettings(**values)


def test_auth_settings_repr_hides_secrets() -> None:
    """Auth settings never expose secret values through their representation."""
    settings = make_auth_settings()

    representation = repr(settings)

    assert BOT_TOKEN not in representation
    assert SECRET not in representation


def _user_and_profile() -> tuple[User, TelegramProfile]:
    """Build an existing domain user with its Telegram profile."""
    user = User(id=7)
    profile = TelegramProfile(
        id=9,
        user_id=7,
        telegram_id=123,
        is_bot=False,
        first_name="Ivan",
        is_premium=False,
    )
    user.telegram_profile = profile
    return user, profile


def _session(
    *,
    session_id: int = 70,
    user_id: int = 7,
    family_id: UUID = FAMILY_ID,
    refresh_token_hash: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> AuthSessionEntity:
    """Build an auth session with deterministic refresh and logout defaults."""
    return AuthSessionEntity(
        id=session_id,
        user_id=user_id,
        family_id=family_id,
        refresh_token_hash=(
            hash_refresh_token("old-refresh-token")
            if refresh_token_hash is None
            else refresh_token_hash
        ),
        expires_at=NOW + timedelta(days=1) if expires_at is None else expires_at,
        revoked_at=revoked_at,
    )


class FakeInitDataValidator:
    """Init-data validator double returning a prepared domain user."""

    def __init__(self, user: TelegramUser | None = None) -> None:
        """
        Store the prepared user and the optional rejection error.

        Purpose:
            Let service tests control validation outcomes without HMAC work.

        Args:
            user: User returned by successful validation. Defaults to the
                standard test user.
        """
        self.user = user if user is not None else TelegramUser(id=123, first_name="Ivan")
        self.error: Exception | None = None
        self.calls: list[str] = []

    def validate(self, raw_init_data: str) -> TelegramUser:
        """Record the raw payload and return or raise the prepared outcome."""
        self.calls.append(raw_init_data)
        if self.error is not None:
            raise self.error
        return self.user


class FakeTokenIssuer:
    """Access-token issuer double that records every issuance request."""

    def __init__(self, access_ttl_seconds: int = 900) -> None:
        """
        Store the access-token TTL used for returned expiry times.

        Purpose:
            Let tests assert the expiry propagated from the issued token pair.

        Args:
            access_ttl_seconds: Access-token lifetime in seconds.
        """
        self.access_ttl_seconds = access_ttl_seconds
        self.calls: list[tuple[int, int, int, datetime]] = []

    def issue_access_token(
        self,
        user_id: int,
        session_id: int,
        telegram_id: int,
        *,
        now: datetime,
    ) -> tuple[str, datetime]:
        """Record the issuance request and return a deterministic token pair."""
        self.calls.append((user_id, session_id, telegram_id, now))
        return "access-token", now + timedelta(seconds=self.access_ttl_seconds)


class RecordingUnitOfWork:
    """Unit-of-work double that records transaction calls in order."""

    def __init__(self, order: list[str] | None = None) -> None:
        """
        Initialize an empty transaction call log.

        Purpose:
            Observe commit boundaries performed explicitly by the auth service
            and, when requested, append them to a log shared with the
            repository so call ordering can be asserted.

        Args:
            order: Optional shared ordered event log receiving commit names.
        """
        self.calls: list[str] = []
        self._order = order

    async def flush(self) -> None:
        """Record a flush request."""
        self.calls.append("flush")

    async def commit(self) -> None:
        """Record a commit request."""
        self.calls.append("commit")
        if self._order is not None:
            self._order.append("commit")

    async def rollback(self) -> None:
        """Record a rollback request."""
        self.calls.append("rollback")


class FakeAuthRepository:
    """In-memory auth repository double that records every call."""

    def __init__(
        self,
        *,
        identity: TelegramIdentity | None = None,
        user: User | None = None,
        next_session_id: int = SESSION_ID,
        session_telegram_id: int = 123,
        order: list[str] | None = None,
    ) -> None:
        """
        Store prepared return values and initialize the call log.

        Purpose:
            Provide deterministic identity, session, and user lookups while
            recording repository interactions.

        Args:
            identity: Existing user/profile pair returned by identity lookup.
            user: User returned by the authenticated-user lookup.
            next_session_id: Identifier assigned to the next created session.
            session_telegram_id: Telegram id returned for a session hash lookup.
            order: Optional shared ordered event log receiving revocation
                operations so commit ordering can be asserted.
        """
        self.events: list[tuple[str, object]] = []
        self.identity = identity
        self.profile = identity.profile if identity is not None else None
        self.user = user if user is not None else identity.user if identity is not None else None
        self.lock_ids: list[int] = []
        self.session_kwargs: dict[str, object] | None = None
        self.session: AuthSessionEntity | None = None
        self.revoked_sessions: list[AuthSessionEntity] = []
        self.revoked_families: list[UUID] = []
        self.session_telegram_id = session_telegram_id
        self._next_session_id = next_session_id
        self._order = order

    @property
    def call_names(self) -> list[str]:
        """Return the recorded method names in call order."""
        return [name for name, _ in self.events]

    async def acquire_telegram_lock(self, telegram_id: int) -> None:
        """Record the advisory lock request for a Telegram user."""
        self.events.append(("acquire_telegram_lock", telegram_id))
        self.lock_ids.append(telegram_id)

    async def get_identity_by_telegram_id(
        self,
        telegram_id: int,
    ) -> TelegramIdentity | None:
        """Record an identity lookup and return the prepared identity."""
        self.events.append(("get_identity_by_telegram_id", telegram_id))
        return self.identity

    async def create_user_with_profile(self, data: TelegramUser) -> TelegramIdentity:
        """Record user creation and return a new identity linked to a new user."""
        self.events.append(("create_user_with_profile", data.id))
        user = User(id=42)
        profile = TelegramProfile(
            id=43,
            user_id=42,
            telegram_id=data.id,
            is_bot=data.is_bot,
            first_name=data.first_name,
            is_premium=data.is_premium,
        )
        user.telegram_profile = profile
        identity = TelegramIdentity(user=user, profile=profile)
        self.identity = identity
        self.user = user
        self.profile = profile
        return identity

    async def update_profile(self, profile: TelegramProfile, data: TelegramUser) -> None:
        """Record a profile update request."""
        self.events.append(("update_profile", (profile, data)))

    async def create_session(
        self,
        *,
        user_id: int,
        family_id: UUID,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> AuthSessionEntity:
        """Record session creation and return a session with a fixed identifier."""
        self.session_kwargs = {
            "user_id": user_id,
            "family_id": family_id,
            "refresh_token_hash": refresh_token_hash,
            "expires_at": expires_at,
        }
        self.events.append(("create_session", self.session_kwargs))
        self.session = AuthSessionEntity(
            id=self._next_session_id,
            user_id=user_id,
            family_id=family_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
        self._next_session_id += 1
        return self.session

    async def get_session_for_update(
        self,
        refresh_token_hash: str,
    ) -> AuthSessionEntity | None:
        """Record a locking session lookup and return the prepared session."""
        self.events.append(("get_session_for_update", refresh_token_hash))
        return self.session

    async def get_telegram_id_for_session(self, refresh_token_hash: str) -> int | None:
        """Record a non-locking Telegram identity lookup for a session hash."""
        self.events.append(("get_telegram_id_for_session", refresh_token_hash))
        if self.session is None:
            return None
        return self.session_telegram_id

    async def revoke_session(
        self,
        session: AuthSessionEntity,
        revoked_at: datetime,
    ) -> None:
        """Record a session revocation request."""
        self.events.append(("revoke_session", (session, revoked_at)))
        self.revoked_sessions.append(session)
        if self._order is not None:
            self._order.append("revoke_session")

    async def revoke_family(self, family_id: UUID, revoked_at: datetime) -> None:
        """Record a family revocation request."""
        self.events.append(("revoke_family", (family_id, revoked_at)))
        self.revoked_families.append(family_id)
        if self._order is not None:
            self._order.append("revoke_family")

    async def revoke_user_sessions(self, user_id: int, revoked_at: datetime) -> None:
        """Record a bulk revocation of every active session of a user."""
        self.events.append(("revoke_user_sessions", (user_id, revoked_at)))

    async def get_user_with_profile(self, user_id: int) -> User | None:
        """Record a user lookup and return the prepared user."""
        self.events.append(("get_user_with_profile", user_id))
        return self.user

    async def update_user_timezone(self, user_id: int, timezone: str) -> User | None:
        """Record a timezone update and return the prepared user."""
        self.events.append(("update_user_timezone", (user_id, timezone)))
        if self.user is None:
            return None
        self.user.timezone = timezone
        return self.user


def make_service(
    repository: FakeAuthRepository,
    *,
    uow: RecordingUnitOfWork | None = None,
    validator: FakeInitDataValidator | None = None,
    token_issuer: FakeTokenIssuer | None = None,
    settings: AuthSettings | None = None,
) -> AuthService:
    """Build the real auth service with deterministic doubles."""
    return AuthService(
        repository,
        uow if uow is not None else RecordingUnitOfWork(),
        validator if validator is not None else FakeInitDataValidator(),
        token_issuer if token_issuer is not None else FakeTokenIssuer(),
        settings if settings is not None else make_auth_settings(),
        clock=clock,
    )


async def test_login_creates_user_with_profile() -> None:
    """Create a user and profile when the Telegram identity is new."""
    repository = FakeAuthRepository()
    service = make_service(repository)

    result = await service.login("raw-init-data")

    assert result.is_new is True
    assert "create_user_with_profile" in repository.call_names
    assert "update_profile" not in repository.call_names
    assert result.user is repository.user
    assert result.profile is repository.profile


async def test_login_updates_existing_profile() -> None:
    """Update an existing profile instead of creating a new user."""
    user, profile = _user_and_profile()
    repository = FakeAuthRepository(identity=TelegramIdentity(user=user, profile=profile))
    service = make_service(repository)

    result = await service.login("raw-init-data")

    assert result.is_new is False
    assert result.user is user
    assert result.profile is profile
    assert "update_profile" in repository.call_names
    assert "create_user_with_profile" not in repository.call_names


async def test_login_acquires_telegram_lock() -> None:
    """Acquire the Telegram advisory lock before looking up the identity."""
    repository = FakeAuthRepository()
    service = make_service(repository)

    await service.login("raw-init-data")

    names = repository.call_names
    assert names.index("acquire_telegram_lock") < names.index("get_identity_by_telegram_id")
    assert repository.lock_ids == [123]


async def test_login_revokes_user_sessions_before_creating_new_session() -> None:
    """Revoke every active session of the user before creating the new one."""
    user, profile = _user_and_profile()
    repository = FakeAuthRepository(identity=TelegramIdentity(user=user, profile=profile))
    service = make_service(repository)

    result = await service.login("raw-init-data")

    assert ("revoke_user_sessions", (result.user.id, NOW)) in repository.events
    names = repository.call_names
    assert names.index("revoke_user_sessions") < names.index("create_session")


async def test_login_creates_session_with_new_family() -> None:
    """Create a session with a new family, hashed token, and expected expiry."""
    settings = make_auth_settings()
    repository = FakeAuthRepository()
    service = make_service(repository, settings=settings)

    result = await service.login("raw-init-data")

    assert repository.session_kwargs is not None
    family_id = repository.session_kwargs["family_id"]
    token_hash = repository.session_kwargs["refresh_token_hash"]
    expires_at = repository.session_kwargs["expires_at"]
    assert isinstance(family_id, UUID)
    assert isinstance(token_hash, str)
    assert len(token_hash) == 64
    assert repository.session_kwargs["user_id"] == result.user.id
    assert expires_at == NOW + timedelta(seconds=settings.auth_refresh_ttl_seconds)
    assert result.tokens.refresh_expires_at == expires_at


async def test_login_does_not_store_raw_refresh() -> None:
    """Pass only the refresh-token hash to the repository."""
    repository = FakeAuthRepository()
    service = make_service(repository)

    result = await service.login("raw-init-data")

    assert repository.session_kwargs is not None
    assert repository.session_kwargs["refresh_token_hash"] == hash_refresh_token(
        result.tokens.refresh_token
    )
    assert result.tokens.refresh_token not in repository.session_kwargs.values()


async def test_login_issues_access_token_through_token_issuer() -> None:
    """Issue the access token for the persisted identity and session."""
    token_issuer = FakeTokenIssuer()
    settings = make_auth_settings()
    repository = FakeAuthRepository()
    service = make_service(repository, token_issuer=token_issuer, settings=settings)

    result = await service.login("raw-init-data")

    assert token_issuer.calls == [(result.user.id, SESSION_ID, 123, NOW)]
    assert result.tokens.access_token == "access-token"
    assert result.tokens.access_expires_at == NOW + timedelta(
        seconds=settings.auth_access_ttl_seconds
    )


async def test_login_rejects_validator_failure_before_repository() -> None:
    """Propagate validation failures before touching the repository."""
    repository = FakeAuthRepository()
    validator = FakeInitDataValidator()
    validator.error = UnauthorizedError("Invalid init data")
    service = make_service(repository, validator=validator)

    with pytest.raises(UnauthorizedError):
        await service.login("tampered")

    assert repository.events == []


async def test_login_requires_bot_token() -> None:
    """Reject login when the Telegram bot token is not configured."""
    repository = FakeAuthRepository()
    service = make_service(repository, settings=make_auth_settings(telegram_bot_token=None))

    with pytest.raises(AuthNotConfiguredError):
        await service.login("raw-init-data")

    assert repository.events == []


async def test_login_requires_jwt_secret() -> None:
    """Reject login when the JWT secret is not configured."""
    repository = FakeAuthRepository()
    service = make_service(repository, settings=make_auth_settings(auth_jwt_secret=None))

    with pytest.raises(AuthNotConfiguredError):
        await service.login("raw-init-data")

    assert repository.events == []


async def test_login_does_not_commit() -> None:
    """Leave the transaction boundary to the request unit of work."""
    uow = RecordingUnitOfWork()
    repository = FakeAuthRepository()
    service = make_service(repository, uow=uow)

    await service.login("raw-init-data")

    assert uow.calls == []


async def test_get_user_delegates_to_repository() -> None:
    """Return the user loaded by the repository."""
    user = User(id=7)
    repository = FakeAuthRepository(user=user)
    service = make_service(repository)

    result = await service.get_user(7)

    assert result is user
    assert repository.events == [("get_user_with_profile", 7)]


async def test_refresh_rotates_tokens_and_family() -> None:
    """Rotate refresh credentials and revoke the previous session."""
    settings = make_auth_settings()
    user, _ = _user_and_profile()
    old_session = _session(session_id=70)
    repository = FakeAuthRepository(user=user)
    repository.session = old_session
    service = make_service(repository, settings=settings)

    result = await service.refresh("old-refresh-token")

    new_session = repository.session
    assert new_session is not None
    assert new_session.id != old_session.id
    assert new_session.family_id == old_session.family_id
    assert new_session.refresh_token_hash != old_session.refresh_token_hash
    assert new_session.refresh_token_hash == hash_refresh_token(result.refresh_token)
    assert ("revoke_session", (old_session, NOW)) in repository.events
    assert result.refresh_expires_at == NOW + timedelta(seconds=settings.auth_refresh_ttl_seconds)


async def test_refresh_issues_access_token_for_rotated_session() -> None:
    """Issue the new access token with the current identity and session."""
    user, profile = _user_and_profile()
    old_session = _session()
    repository = FakeAuthRepository(user=user)
    repository.session = old_session
    token_issuer = FakeTokenIssuer()
    service = make_service(repository, token_issuer=token_issuer)

    result = await service.refresh("old-refresh-token")

    assert repository.session is not None
    assert token_issuer.calls == [(user.id, repository.session.id, profile.telegram_id, NOW)]
    assert result.access_token == "access-token"


async def test_refresh_locks_telegram_before_session_row() -> None:
    """Look up the Telegram identity and lock it before locking the session row."""
    repository = FakeAuthRepository(user=_user_and_profile()[0])
    repository.session = _session()
    service = make_service(repository)

    await service.refresh("old-refresh-token")

    assert repository.call_names[:3] == [
        "get_telegram_id_for_session",
        "acquire_telegram_lock",
        "get_session_for_update",
    ]
    assert repository.events[0] == (
        "get_telegram_id_for_session",
        hash_refresh_token("old-refresh-token"),
    )
    assert repository.lock_ids == [123]


async def test_refresh_rejects_unknown_token() -> None:
    """Reject a refresh token that has no stored session."""
    repository = FakeAuthRepository()
    uow = RecordingUnitOfWork()
    service = make_service(repository, uow=uow)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("unknown-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert repository.call_names == ["get_telegram_id_for_session"]
    assert repository.lock_ids == []
    assert uow.calls == []


async def test_refresh_expired_session_is_revoked_and_committed() -> None:
    """Revoke an expired session, commit, then reject the request."""
    order: list[str] = []
    old_session = _session(expires_at=NOW)
    repository = FakeAuthRepository(order=order)
    repository.session = old_session
    uow = RecordingUnitOfWork(order=order)
    service = make_service(repository, uow=uow)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    order.append("raised")

    assert str(exc_info.value) == "Invalid refresh token"
    assert repository.revoked_sessions == [old_session]
    assert uow.calls == ["commit"]
    assert order == ["revoke_session", "commit", "raised"]
    assert "create_session" not in repository.call_names


async def test_refresh_reuse_outside_grace_commits_revocation_before_raise() -> None:
    """Reuse detection persists the family revocation, commits, then rejects."""
    order: list[str] = []
    old_session = _session(revoked_at=NOW - timedelta(seconds=31))
    repository = FakeAuthRepository(order=order)
    repository.session = old_session
    uow = RecordingUnitOfWork(order=order)
    service = make_service(repository, uow=uow)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    order.append("raised")

    assert type(exc_info.value) is UnauthorizedError
    assert repository.revoked_families == [FAMILY_ID]
    assert uow.calls == ["commit"]
    assert order == ["revoke_family", "commit", "raised"]
    assert "create_session" not in repository.call_names


async def test_refresh_reuse_within_grace_does_not_revoke_family() -> None:
    """Reject a just-rotated token without revoking its family or committing."""
    old_session = _session(revoked_at=NOW - timedelta(seconds=10))
    repository = FakeAuthRepository()
    repository.session = old_session
    uow = RecordingUnitOfWork()
    service = make_service(repository, uow=uow)

    with pytest.raises(RefreshTokenGraceError) as exc_info:
        await service.refresh("old-refresh-token")

    error = exc_info.value
    assert str(error) == "Invalid refresh token"
    assert error.code is ErrorCode.UNAUTHORIZED
    assert error.headers == {"WWW-Authenticate": "Bearer"}
    assert not any(name.startswith("revoke") for name in repository.call_names)
    assert "create_session" not in repository.call_names
    assert uow.calls == []


async def test_refresh_rotation_does_not_commit() -> None:
    """Leave the successful rotation transaction to the request unit of work."""
    repository = FakeAuthRepository(user=_user_and_profile()[0])
    repository.session = _session()
    uow = RecordingUnitOfWork()
    service = make_service(repository, uow=uow)

    await service.refresh("old-refresh-token")

    assert uow.calls == []


async def test_refresh_keeps_family_id() -> None:
    """Create the rotated session in the original refresh-token family."""
    family_id = UUID("00000000-0000-0000-0000-000000000002")
    old_session = _session(family_id=family_id)
    repository = FakeAuthRepository(user=_user_and_profile()[0])
    repository.session = old_session
    service = make_service(repository)

    await service.refresh("old-refresh-token")

    assert repository.session_kwargs is not None
    assert repository.session_kwargs["family_id"] == family_id
    assert repository.session is not None
    assert repository.session.family_id == family_id


async def test_refresh_rejects_missing_user() -> None:
    """Reject refresh when the session user no longer exists."""
    repository = FakeAuthRepository()
    repository.session = _session()
    service = make_service(repository)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert "create_session" not in repository.call_names


async def test_refresh_rejects_user_without_telegram_profile() -> None:
    """Reject refresh when the user has no Telegram profile."""
    repository = FakeAuthRepository(user=User(id=7))
    repository.session = _session()
    service = make_service(repository)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert "create_session" not in repository.call_names


async def test_refresh_requires_configuration_before_repository() -> None:
    """Reject refresh before repository work when authentication is unconfigured."""
    repository = FakeAuthRepository()
    service = make_service(repository, settings=make_auth_settings(auth_jwt_secret=None))

    with pytest.raises(AuthNotConfiguredError):
        await service.refresh("old-refresh-token")

    assert repository.events == []


async def test_logout_revokes_family_by_refresh_token() -> None:
    """Revoke the refresh-token family resolved from the raw token."""
    old_session = _session()
    repository = FakeAuthRepository()
    repository.session = old_session
    service = make_service(repository)

    await service.logout("old-refresh-token")

    assert repository.events == [
        ("get_session_for_update", hash_refresh_token("old-refresh-token")),
        ("revoke_family", (old_session.family_id, NOW)),
    ]


async def test_logout_with_unknown_token_is_idempotent() -> None:
    """Ignore logout for a refresh token that has no stored session."""
    repository = FakeAuthRepository()
    service = make_service(repository)

    await service.logout("unknown-refresh-token")

    assert repository.call_names == ["get_session_for_update"]


async def test_logout_without_token_is_idempotent() -> None:
    """Skip repository work when logout has no refresh token."""
    repository = FakeAuthRepository()
    service = make_service(repository)

    await service.logout(None)

    assert repository.events == []


async def test_logout_does_not_commit() -> None:
    """Leave the logout transaction to the request unit of work."""
    repository = FakeAuthRepository()
    repository.session = _session()
    uow = RecordingUnitOfWork()
    service = make_service(repository, uow=uow)

    await service.logout("old-refresh-token")

    assert uow.calls == []


async def test_refresh_does_not_expose_raw_old_token() -> None:
    """Pass only hashes to the repository during rotation."""
    repository = FakeAuthRepository(user=_user_and_profile()[0])
    repository.session = _session()
    service = make_service(repository)

    result = await service.refresh("old-refresh-token")

    assert ("get_session_for_update", hash_refresh_token("old-refresh-token")) in repository.events
    assert "old-refresh-token" not in str(repository.events)
    assert result.refresh_token not in str(repository.events)


async def test_update_timezone_delegates_to_repository() -> None:
    """Validate the zone, persist it, and return the updated user."""
    user = User(id=7, timezone="UTC")
    repository = FakeAuthRepository(user=user)
    uow = RecordingUnitOfWork()
    service = make_service(repository, uow=uow)

    result = await service.update_timezone(7, "Europe/Moscow")

    assert result is user
    assert user.timezone == "Europe/Moscow"
    assert repository.events == [("update_user_timezone", (7, "Europe/Moscow"))]
    assert uow.calls == []


async def test_update_timezone_rejects_invalid_zone() -> None:
    """Reject an unknown IANA zone before touching the repository."""
    repository = FakeAuthRepository(user=User(id=7))
    service = make_service(repository)

    with pytest.raises(BadRequestError) as exc_info:
        await service.update_timezone(7, "Mars/Olympus")

    assert exc_info.value.code is ErrorCode.BAD_REQUEST
    assert repository.events == []


async def test_update_timezone_rejects_missing_user() -> None:
    """Reject a timezone update when the user no longer exists."""
    repository = FakeAuthRepository()
    service = make_service(repository)

    with pytest.raises(UnauthorizedError):
        await service.update_timezone(7, "Europe/Moscow")
