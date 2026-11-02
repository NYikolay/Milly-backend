from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from backend.core.config import Settings, get_settings


def test_defaults_are_local_and_docs_enabled() -> None:
    """Use local defaults and enable docs by default."""
    settings = Settings(_env_file=None)
    assert settings.app_env == "local"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.direct_database_url.startswith("postgresql+asyncpg://")
    assert settings.docs_enabled is True


def test_docs_disabled_in_production_by_default() -> None:
    """Disable docs in production when ENABLE_DOCS is unset."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        telegram_bot_token="123:token",
        auth_jwt_secret="a" * 32,
    )
    assert settings.docs_enabled is False


def test_enable_docs_overrides_production_default() -> None:
    """Let ENABLE_DOCS override the production default."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        enable_docs=True,
        telegram_bot_token="123:token",
        auth_jwt_secret="a" * 32,
    )
    assert settings.docs_enabled is True


def test_database_url_must_use_asyncpg() -> None:
    """Reject DATABASE_URL values without the asyncpg driver."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://user:pass@localhost/db")


def test_direct_database_url_must_use_asyncpg() -> None:
    """Reject DIRECT_DATABASE_URL values without the asyncpg driver."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            direct_database_url="postgresql://user:pass@localhost/db",
        )


def test_production_forbids_debug() -> None:
    """Reject DEBUG=true in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            debug=True,
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
        )


def test_local_auth_settings_are_optional() -> None:
    """Keep Telegram and JWT credentials optional outside production."""
    settings = Settings(_env_file=None)
    assert settings.telegram_bot_token is None
    assert settings.auth_jwt_secret is None
    assert settings.auth_access_ttl_seconds == 900
    assert settings.auth_refresh_ttl_seconds == 2592000
    assert settings.auth_refresh_cookie_samesite == "lax"


def test_production_requires_telegram_bot_token() -> None:
    """Require a Telegram bot token in production."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", auth_jwt_secret="a" * 32)


def test_production_requires_strong_jwt_secret() -> None:
    """Reject a JWT secret shorter than 32 characters in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="short",
        )


def test_production_rejects_dev_telegram_token() -> None:
    """Reject a bot token without the bot-id separator in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="dev-telegram-bot-token",
            auth_jwt_secret="a" * 32,
        )


def test_production_rejects_dev_jwt_secret() -> None:
    """Reject a placeholder JWT secret in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="dev-jwt-secret-change-me-32-chars",
        )


def test_production_requires_secure_cookie() -> None:
    """Require the refresh cookie to be Secure in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
            auth_refresh_cookie_secure=False,
        )


def test_production_rejects_samesite_none() -> None:
    """Reject SameSite=None for the same-site refresh cookie in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
            auth_refresh_cookie_samesite="none",
        )


def test_production_accepts_auth_configuration() -> None:
    """Accept a complete production auth configuration."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        telegram_bot_token="123:token",
        auth_jwt_secret="a" * 32,
    )
    assert isinstance(settings.telegram_bot_token, SecretStr)
    assert isinstance(settings.auth_jwt_secret, SecretStr)


def test_auth_ttls_must_be_positive() -> None:
    """Reject non-positive access and refresh token lifetimes."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_access_ttl_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_refresh_ttl_seconds=0)


def test_auth_refresh_grace_must_be_positive() -> None:
    """Reject a non-positive refresh reuse grace window."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, auth_refresh_reuse_grace_seconds=0)


def test_extra_env_is_ignored() -> None:
    """Ignore unknown environment keys such as provider credentials."""
    settings = Settings(_env_file=None, provider_api_key="ignored")
    assert settings.app_env == "local"


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Return and cache one Settings instance without loading a local .env."""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()
