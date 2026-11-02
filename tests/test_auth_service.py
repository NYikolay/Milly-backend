from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from backend.auth.errors import AuthNotConfiguredError, RefreshTokenGraceError
from backend.auth.init_data import TelegramUser
from backend.auth.service import AuthService
from backend.auth.tokens import decode_access_token, hash_refresh_token
from backend.core.config import Settings
from backend.core.errors import ErrorCode, UnauthorizedError
from backend.models.auth_session import AuthSession
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User
from tests.auth_helpers import BOT_TOKEN, make_init_data

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SECRET = "a" * 32
SESSION_ID = 77
FAMILY_ID = UUID("00000000-0000-0000-0000-000000000001")


def clock() -> datetime:
    """Return the fixed test clock."""
    return NOW


def _settings(**overrides: Any) -> Settings:
    """Build auth settings with deterministic values."""
    values: dict[str, Any] = {
        "_env_file": None,
        "telegram_bot_token": BOT_TOKEN,
        "auth_jwt_secret": SECRET,
        "telegram_init_data_max_age_seconds": 3600,
    }
    values.update(overrides)
    return Settings(**values)


def _user_and_profile() -> tuple[User, TelegramProfile]:
    """Build an existing user with its Telegram profile."""
    user = User(id=7)
    profile = TelegramProfile(
        id=9,
        user=user,
        user_id=7,
        telegram_id=123,
        is_bot=False,
        first_name="Ivan",
        is_premium=False,
    )
    return user, profile


def _session(
    *,
    session_id: int = 70,
    user_id: int = 7,
    family_id: UUID = FAMILY_ID,
    refresh_token_hash: str | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> AuthSession:
    """Build an auth session with deterministic refresh and logout defaults."""
    return AuthSession(
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


class FakeAuthRepository:
    """In-memory auth repository double that records every call."""

    def __init__(
        self,
        *,
        profile: TelegramProfile | None = None,
        user: User | None = None,
        next_session_id: int = SESSION_ID,
    ) -> None:
        """Store prepared return values and initialize the call log."""
        self.events: list[tuple[str, object]] = []
        self.profile = profile
        self.user = user
        self.lock_ids: list[int] = []
        self.session_kwargs: dict[str, object] | None = None
        self.session: AuthSession | None = None
        self._next_session_id = next_session_id

    @property
    def call_names(self) -> list[str]:
        """Return the recorded method names in call order."""
        return [name for name, _ in self.events]

    async def acquire_telegram_lock(self, telegram_id: int) -> None:
        """Record the advisory lock request for a Telegram user."""
        self.events.append(("acquire_telegram_lock", telegram_id))
        self.lock_ids.append(telegram_id)

    async def get_profile_by_telegram_id(self, telegram_id: int) -> TelegramProfile | None:
        """Record a profile lookup and return the prepared profile."""
        self.events.append(("get_profile_by_telegram_id", telegram_id))
        return self.profile

    async def create_user_with_profile(self, data: TelegramUser) -> TelegramProfile:
        """Record user creation and return a new profile linked to a new user."""
        self.events.append(("create_user_with_profile", data.id))
        user = User(id=42)
        profile = TelegramProfile(
            id=43,
            user=user,
            user_id=42,
            telegram_id=data.id,
            is_bot=data.is_bot,
            first_name=data.first_name,
            is_premium=data.is_premium,
        )
        self.user = user
        self.profile = profile
        return profile

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
    ) -> AuthSession:
        """Record session creation and return a session with a fixed identifier."""
        self.session_kwargs = {
            "user_id": user_id,
            "family_id": family_id,
            "refresh_token_hash": refresh_token_hash,
            "expires_at": expires_at,
        }
        self.events.append(("create_session", self.session_kwargs))
        self.session = AuthSession(
            id=self._next_session_id,
            user_id=user_id,
            family_id=family_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
        self._next_session_id += 1
        return self.session

    async def get_session_for_update(self, refresh_token_hash: str) -> AuthSession | None:
        """Record a locking session lookup and return the prepared session."""
        self.events.append(("get_session_for_update", refresh_token_hash))
        return self.session

    async def get_session_by_token_hash(self, refresh_token_hash: str) -> AuthSession | None:
        """Record a non-locking session lookup and return the prepared session."""
        self.events.append(("get_session_by_token_hash", refresh_token_hash))
        return self.session

    async def revoke_session(self, session: AuthSession, revoked_at: datetime) -> None:
        """Record a session revocation request."""
        self.events.append(("revoke_session", (session, revoked_at)))

    async def revoke_family(self, family_id: UUID, revoked_at: datetime) -> None:
        """Record a family revocation request."""
        self.events.append(("revoke_family", (family_id, revoked_at)))

    async def revoke_session_and_commit(
        self,
        session: AuthSession,
        revoked_at: datetime,
    ) -> None:
        """Record a committing session revocation request."""
        self.events.append(("revoke_session_and_commit", (session, revoked_at)))

    async def revoke_family_and_commit(self, family_id: UUID, revoked_at: datetime) -> None:
        """Record a committing family revocation request."""
        self.events.append(("revoke_family_and_commit", (family_id, revoked_at)))

    async def get_user_with_profile(self, user_id: int) -> User | None:
        """Record a user lookup and return the prepared user."""
        self.events.append(("get_user_with_profile", user_id))
        return self.user


async def test_login_creates_user_with_profile() -> None:
    """Create a user and profile when the Telegram identity is new."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)

    result = await service.login(make_init_data())

    assert result.is_new is True
    assert "create_user_with_profile" in repository.call_names
    assert "update_profile" not in repository.call_names
    assert result.user is result.profile.user


async def test_login_updates_existing_profile() -> None:
    """Update an existing profile instead of creating a new user."""
    user, profile = _user_and_profile()
    repository = FakeAuthRepository(profile=profile)
    service = AuthService(repository, _settings(), clock=clock)

    result = await service.login(make_init_data())

    assert result.is_new is False
    assert result.user is user
    assert "update_profile" in repository.call_names
    assert "create_user_with_profile" not in repository.call_names


async def test_login_acquires_telegram_lock() -> None:
    """Acquire the Telegram advisory lock before looking up the profile."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)

    await service.login(make_init_data())

    names = repository.call_names
    assert names.index("acquire_telegram_lock") < names.index("get_profile_by_telegram_id")
    assert repository.lock_ids == [123]


async def test_login_creates_session_with_new_family() -> None:
    """Create a session with a new family, hashed token, and expected expiry."""
    settings = _settings()
    repository = FakeAuthRepository()
    service = AuthService(repository, settings, clock=clock)

    result = await service.login(make_init_data())

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
    service = AuthService(repository, _settings(), clock=clock)

    result = await service.login(make_init_data())

    assert repository.session_kwargs is not None
    assert repository.session_kwargs["refresh_token_hash"] == hash_refresh_token(
        result.tokens.refresh_token
    )
    assert result.tokens.refresh_token not in repository.session_kwargs.values()


async def test_login_issues_access_claims() -> None:
    """Issue an access token carrying the created identity and session claims."""
    settings = _settings()
    repository = FakeAuthRepository()
    service = AuthService(repository, settings, clock=clock)

    result = await service.login(make_init_data())

    claims = decode_access_token(result.tokens.access_token, settings=settings, now=NOW)
    assert claims.user_id == result.user.id
    assert claims.session_id == SESSION_ID
    assert claims.telegram_id == 123
    assert result.tokens.access_expires_at == NOW + timedelta(
        seconds=settings.auth_access_ttl_seconds
    )


async def test_login_rejects_bot_user() -> None:
    """Reject bot users before touching the repository."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)
    raw_init_data = make_init_data(user={"id": 123, "first_name": "Bot", "is_bot": True})

    with pytest.raises(UnauthorizedError):
        await service.login(raw_init_data)

    assert repository.events == []


async def test_login_requires_bot_token() -> None:
    """Reject login when the Telegram bot token is not configured."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(telegram_bot_token=None), clock=clock)

    with pytest.raises(AuthNotConfiguredError):
        await service.login(make_init_data())

    assert repository.events == []


async def test_login_requires_jwt_secret() -> None:
    """Reject login when the JWT secret is not configured."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(auth_jwt_secret=None), clock=clock)

    with pytest.raises(AuthNotConfiguredError):
        await service.login(make_init_data())

    assert repository.events == []


async def test_login_with_invalid_init_data_does_not_touch_repository() -> None:
    """Reject tampered init data before any repository call."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)
    raw_init_data = make_init_data()
    tampered = f"{raw_init_data[:-1]}{'0' if raw_init_data[-1] != '0' else '1'}"

    with pytest.raises(UnauthorizedError):
        await service.login(tampered)

    assert repository.events == []


async def test_get_user_delegates_to_repository() -> None:
    """Return the user loaded by the repository."""
    user = User(id=7)
    repository = FakeAuthRepository(user=user)
    service = AuthService(repository, _settings(), clock=clock)

    result = await service.get_user(7)

    assert result is user
    assert repository.events == [("get_user_with_profile", 7)]


async def test_refresh_rotates_tokens_and_family() -> None:
    """Rotate refresh credentials and revoke the previous session."""
    settings = _settings()
    user, _ = _user_and_profile()
    old_session = _session(session_id=70)
    repository = FakeAuthRepository(user=user)
    repository.session = old_session
    service = AuthService(repository, settings, clock=clock)

    result = await service.refresh("old-refresh-token")

    new_session = repository.session
    assert new_session is not None
    assert new_session.id != old_session.id
    assert new_session.family_id == old_session.family_id
    assert new_session.refresh_token_hash != old_session.refresh_token_hash
    assert new_session.refresh_token_hash == hash_refresh_token(result.refresh_token)
    assert ("revoke_session", (old_session, NOW)) in repository.events
    claims = decode_access_token(result.access_token, settings=settings, now=NOW)
    assert claims.session_id == new_session.id
    assert result.refresh_expires_at == NOW + timedelta(seconds=settings.auth_refresh_ttl_seconds)


async def test_refresh_rejects_unknown_token() -> None:
    """Reject a refresh token that has no stored session."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("unknown-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert repository.call_names == ["get_session_for_update"]


async def test_refresh_rejects_expired_and_revokes() -> None:
    """Revoke an expired session before rejecting the refresh request."""
    old_session = _session(expires_at=NOW)
    repository = FakeAuthRepository()
    repository.session = old_session
    service = AuthService(repository, _settings(), clock=clock)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert ("revoke_session_and_commit", (old_session, NOW)) in repository.events
    assert "create_session" not in repository.call_names


async def test_refresh_reuse_within_grace_does_not_revoke_family() -> None:
    """Reject a just-rotated token without revoking its family."""
    old_session = _session(revoked_at=NOW - timedelta(seconds=10))
    repository = FakeAuthRepository()
    repository.session = old_session
    service = AuthService(repository, _settings(), clock=clock)

    with pytest.raises(RefreshTokenGraceError) as exc_info:
        await service.refresh("old-refresh-token")

    error = exc_info.value
    assert str(error) == "Invalid refresh token"
    assert error.code is ErrorCode.UNAUTHORIZED
    assert error.headers == {"WWW-Authenticate": "Bearer"}
    assert not any(name.startswith("revoke") for name in repository.call_names)
    assert "create_session" not in repository.call_names


async def test_refresh_reuse_outside_grace_revokes_family() -> None:
    """Revoke the whole family when a stale rotated token is reused."""
    old_session = _session(revoked_at=NOW - timedelta(seconds=31))
    repository = FakeAuthRepository()
    repository.session = old_session
    service = AuthService(repository, _settings(), clock=clock)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    assert type(exc_info.value) is UnauthorizedError
    assert str(exc_info.value) == "Invalid refresh token"
    assert ("revoke_family_and_commit", (old_session.family_id, NOW)) in repository.events
    assert "create_session" not in repository.call_names


async def test_refresh_keeps_family_id() -> None:
    """Create the rotated session in the original refresh-token family."""
    family_id = UUID("00000000-0000-0000-0000-000000000002")
    old_session = _session(family_id=family_id)
    repository = FakeAuthRepository(user=_user_and_profile()[0])
    repository.session = old_session
    service = AuthService(repository, _settings(), clock=clock)

    await service.refresh("old-refresh-token")

    assert repository.session_kwargs is not None
    assert repository.session_kwargs["family_id"] == family_id
    assert repository.session is not None
    assert repository.session.family_id == family_id


async def test_refresh_loads_user_for_telegram_id() -> None:
    """Issue the new access token with the user's current Telegram identity."""
    user, profile = _user_and_profile()
    old_session = _session()
    repository = FakeAuthRepository(user=user)
    repository.session = old_session
    settings = _settings()
    service = AuthService(repository, settings, clock=clock)

    result = await service.refresh("old-refresh-token")

    assert ("get_user_with_profile", old_session.user_id) in repository.events
    claims = decode_access_token(result.access_token, settings=settings, now=NOW)
    assert claims.telegram_id == profile.telegram_id
    assert claims.user_id == user.id


async def test_refresh_rejects_missing_user() -> None:
    """Reject refresh when the session user no longer exists."""
    repository = FakeAuthRepository()
    repository.session = _session()
    service = AuthService(repository, _settings(), clock=clock)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert "create_session" not in repository.call_names


async def test_refresh_rejects_user_without_telegram_profile() -> None:
    """Reject refresh when the user has no Telegram profile."""
    repository = FakeAuthRepository(user=User(id=7))
    repository.session = _session()
    service = AuthService(repository, _settings(), clock=clock)

    with pytest.raises(UnauthorizedError) as exc_info:
        await service.refresh("old-refresh-token")

    assert str(exc_info.value) == "Invalid refresh token"
    assert "create_session" not in repository.call_names


async def test_refresh_requires_configuration_before_repository() -> None:
    """Reject refresh before repository work when authentication is unconfigured."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(auth_jwt_secret=None), clock=clock)

    with pytest.raises(AuthNotConfiguredError):
        await service.refresh("old-refresh-token")

    assert repository.events == []


async def test_logout_revokes_family_by_refresh_token() -> None:
    """Revoke the refresh-token family resolved from the raw token."""
    old_session = _session()
    repository = FakeAuthRepository()
    repository.session = old_session
    service = AuthService(repository, _settings(), clock=clock)

    await service.logout("old-refresh-token")

    assert repository.events == [
        ("get_session_by_token_hash", hash_refresh_token("old-refresh-token")),
        ("revoke_family", (old_session.family_id, NOW)),
    ]


async def test_logout_with_unknown_token_is_idempotent() -> None:
    """Ignore logout for a refresh token that has no stored session."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)

    await service.logout("unknown-refresh-token")

    assert repository.call_names == ["get_session_by_token_hash"]


async def test_logout_without_token_is_idempotent() -> None:
    """Skip repository work when logout has no refresh token."""
    repository = FakeAuthRepository()
    service = AuthService(repository, _settings(), clock=clock)

    await service.logout(None)

    assert repository.events == []


async def test_refresh_does_not_expose_raw_old_token() -> None:
    """Pass only hashes to the repository during rotation."""
    repository = FakeAuthRepository(user=_user_and_profile()[0])
    repository.session = _session()
    service = AuthService(repository, _settings(), clock=clock)

    result = await service.refresh("old-refresh-token")

    assert ("get_session_for_update", hash_refresh_token("old-refresh-token")) in repository.events
    assert "old-refresh-token" not in str(repository.events)
    assert result.refresh_token not in str(repository.events)
