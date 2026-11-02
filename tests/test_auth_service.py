from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from backend.auth.errors import AuthNotConfiguredError
from backend.auth.init_data import TelegramUser
from backend.auth.service import AuthService
from backend.auth.tokens import decode_access_token, hash_refresh_token
from backend.core.config import Settings
from backend.core.errors import UnauthorizedError
from backend.models.auth_session import AuthSession
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User
from tests.auth_helpers import BOT_TOKEN, make_init_data

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SECRET = "a" * 32
SESSION_ID = 77


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


class FakeAuthRepository:
    """In-memory auth repository double that records every call."""

    def __init__(
        self,
        *,
        profile: TelegramProfile | None = None,
        user: User | None = None,
    ) -> None:
        """Store prepared return values and initialize the call log."""
        self.events: list[tuple[str, object]] = []
        self.profile = profile
        self.user = user
        self.lock_ids: list[int] = []
        self.session_kwargs: dict[str, object] | None = None
        self.session: AuthSession | None = None

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
            id=SESSION_ID,
            user_id=user_id,
            family_id=family_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
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
