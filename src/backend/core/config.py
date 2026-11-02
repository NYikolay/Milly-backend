from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Milly API"
    app_env: Literal["local", "dev", "staging", "production"] = "local"
    debug: bool = False
    api_v1_str: str = "/api/v1"
    enable_docs: bool | None = None

    log_level: str = "INFO"
    log_json: bool = True
    cors_origins: list[str] = []
    allowed_hosts: list[str] = ["*"]

    database_url: str = "postgresql+asyncpg://milly:milly@localhost:6432/milly"
    direct_database_url: str = "postgresql+asyncpg://milly:milly@localhost:5432/milly"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle: int = 1800
    sql_echo: bool = False

    nats_url: str = "nats://localhost:4222"
    nats_user: str | None = None
    nats_password: str | None = None
    nats_stream: str = "MILLY_TASKS"
    nats_subject: str = "milly.tasks"
    nats_durable: str = "milly-workers"
    nats_stream_max_age: int = 604800
    nats_ack_wait: int = 300
    nats_max_deliver: int = 5

    redis_url: str = "redis://localhost:6379/0"
    taskiq_result_ex_time: int = 86400
    taskiq_keep_results: bool = True

    @field_validator("database_url", "direct_database_url")
    @classmethod
    def validate_asyncpg_url(cls, value: str) -> str:
        """
        Validate that a PostgreSQL URL uses the asyncpg driver.

        Purpose:
            Ensures the application only accepts async SQLAlchemy connection URLs.

        Args:
            value: Candidate database URL.

        Returns:
            str: The original URL when it is valid.

        Raises:
            ValueError: If the URL does not start with ``postgresql+asyncpg://``.
        """
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("PostgreSQL URL must start with postgresql+asyncpg://")
        return value

    @model_validator(mode="after")
    def validate_production_flags(self) -> Settings:
        """
        Reject debug mode in production.

        Purpose:
            Keeps error details from being exposed by production deployments.

        Returns:
            Settings: The validated settings instance.

        Raises:
            ValueError: If ``app_env`` is ``production`` and ``debug`` is true.
        """
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        return self

    @property
    def docs_enabled(self) -> bool:
        """Return whether OpenAPI/docs routes are enabled."""
        if self.enable_docs is not None:
            return self.enable_docs
        return self.app_env != "production"


@lru_cache
def get_settings() -> Settings:
    """
    Return the cached application settings singleton.

    Purpose:
        Provides one settings instance per process and lets tests clear the cache.

    Returns:
        Settings: Cached settings built from the environment.

    Raises:
        pydantic.ValidationError: If environment values fail validation.

    Notes:
        - The result is memoized with ``lru_cache``; call ``cache_clear()`` to reload.
    """
    return Settings()
