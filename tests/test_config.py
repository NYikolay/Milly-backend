from pathlib import Path

import pytest
from pydantic import ValidationError

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
    settings = Settings(_env_file=None, app_env="production")
    assert settings.docs_enabled is False


def test_enable_docs_overrides_production_default() -> None:
    """Let ENABLE_DOCS override the production default."""
    settings = Settings(_env_file=None, app_env="production", enable_docs=True)
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
        Settings(_env_file=None, app_env="production", debug=True)


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
