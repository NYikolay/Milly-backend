from pathlib import Path
from typing import Any

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


def test_admin_is_disabled_by_default() -> None:
    """Keep the admin panel disabled when no credentials are configured."""
    settings = Settings(_env_file=None)
    assert settings.admin_username == "admin"
    assert settings.admin_password is None
    assert settings.admin_session_max_age_seconds == 43_200
    assert settings.admin_enabled is False


def test_admin_enabled_requires_password_and_jwt_secret() -> None:
    """Enable the admin panel only when both credentials are present."""
    assert Settings(_env_file=None, admin_password="s" * 16).admin_enabled is False
    assert Settings(_env_file=None, auth_jwt_secret="a" * 32).admin_enabled is False
    assert (
        Settings(
            _env_file=None,
            admin_password="s" * 16,
            auth_jwt_secret="",
        ).admin_enabled
        is False
    )
    assert (
        Settings(
            _env_file=None,
            admin_password="s" * 16,
            auth_jwt_secret="a" * 32,
        ).admin_enabled
        is True
    )


def test_admin_password_of_spaces_disables_admin() -> None:
    """Treat a whitespace-only admin password as not configured."""
    settings = Settings(
        _env_file=None,
        admin_password=" " * 16,
        auth_jwt_secret="a" * 32,
    )
    assert settings.admin_enabled is False


def test_production_rejects_short_admin_password() -> None:
    """Reject an admin password shorter than 16 characters in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
            admin_password="short",
        )


def test_production_rejects_change_me_admin_password() -> None:
    """Reject a placeholder admin password in any case in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
            admin_password="Change-Me-Admin-Password",
        )


def test_production_rejects_whitespace_admin_password() -> None:
    """Reject a whitespace-only admin password in production."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
            admin_password=" " * 16,
        )


def test_production_accepts_strong_admin_password() -> None:
    """Accept a strong admin password in production."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        telegram_bot_token="123:token",
        auth_jwt_secret="a" * 32,
        admin_password="correct-horse-battery-staple",
    )
    assert settings.admin_enabled is True


def test_admin_session_max_age_must_be_positive() -> None:
    """Reject a non-positive admin session lifetime."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_session_max_age_seconds=0)


def test_admin_session_max_age_rejects_negative_value() -> None:
    """Reject a negative admin session lifetime."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_session_max_age_seconds=-1)


def test_admin_username_must_not_be_empty() -> None:
    """Reject an empty admin username."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_username="")


def test_explicit_empty_admin_password_keeps_admin_disabled() -> None:
    """Treat an explicitly empty ADMIN_PASSWORD as not configured."""
    settings = Settings(
        _env_file=None,
        admin_password="",
        auth_jwt_secret="a" * 32,
    )
    assert settings.admin_enabled is False


def test_production_rejects_whitespace_jwt_secret() -> None:
    """Reject a whitespace-only JWT secret in production."""
    with pytest.raises(
        ValidationError,
        match="AUTH_JWT_SECRET must be a strong secret in production",
    ):
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token="123:token",
            auth_jwt_secret=" " * 32,
        )


def test_production_validation_hides_secrets_on_weak_admin_password() -> None:
    """Keep configured secrets out of errors for a weak admin password."""
    admin_password = "leaky-admin-pw"
    jwt_secret = "j" * 32
    telegram_token = "123:telegram-secret-token"
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token=telegram_token,
            auth_jwt_secret=jwt_secret,
            admin_password=admin_password,
        )
    message = str(exc_info.value)
    assert "ADMIN_PASSWORD must be a strong password in production" in message
    assert admin_password not in message
    assert jwt_secret not in message
    assert telegram_token not in message


def test_production_validation_hides_secrets_on_unrelated_failure() -> None:
    """Keep configured secrets out of errors for an insecure refresh cookie."""
    admin_password = "leaky-admin-password"
    jwt_secret = "j" * 32
    telegram_token = "123:telegram-secret-token"
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            app_env="production",
            telegram_bot_token=telegram_token,
            auth_jwt_secret=jwt_secret,
            admin_password=admin_password,
            auth_refresh_cookie_secure=False,
        )
    message = str(exc_info.value)
    assert "AUTH_REFRESH_COOKIE_SECURE must be true in production" in message
    assert admin_password not in message
    assert jwt_secret not in message
    assert telegram_token not in message


def test_db_timeout_defaults() -> None:
    """Use the default database statement and pool timeouts."""
    settings = Settings(_env_file=None)
    assert settings.db_statement_timeout_ms == 30_000
    assert settings.db_pool_timeout_seconds == 10


def test_db_timeout_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read database timeouts from environment variables."""
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "5000")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "3")
    settings = Settings(_env_file=None)
    assert settings.db_statement_timeout_ms == 5000
    assert settings.db_pool_timeout_seconds == 3


@pytest.mark.parametrize(
    "field",
    ["db_statement_timeout_ms", "db_pool_timeout_seconds"],
)
@pytest.mark.parametrize("value", [0, -1])
def test_db_timeouts_must_be_positive(field: str, value: int) -> None:
    """Reject non-positive database timeout settings."""
    settings_kwargs: dict[str, Any] = {field: value}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **settings_kwargs)
