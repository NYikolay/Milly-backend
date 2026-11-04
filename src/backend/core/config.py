from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
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
    max_request_body_bytes: int = Field(default=1_048_576, gt=0)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=60, gt=0)
    rate_limit_window_seconds: int = Field(default=60, gt=0)

    database_url: str = "postgresql+asyncpg://milly:milly@localhost:6432/milly"
    direct_database_url: str = "postgresql+asyncpg://milly:milly@localhost:5432/milly"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle: int = 1800
    db_statement_timeout_ms: int = Field(default=30_000, gt=0)
    db_pool_timeout_seconds: int = Field(default=10, gt=0)
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

    telegram_bot_token: SecretStr | None = None
    telegram_init_data_max_age_seconds: int = Field(default=3600, gt=0)
    telegram_init_data_max_future_skew_seconds: int = Field(default=60, ge=0)

    auth_jwt_secret: SecretStr | None = None
    auth_jwt_issuer: str = "milly"
    auth_jwt_leeway_seconds: int = Field(default=30, ge=0)
    auth_access_ttl_seconds: int = Field(default=900, gt=0)
    auth_refresh_ttl_seconds: int = Field(default=2592000, gt=0)
    auth_refresh_reuse_grace_seconds: int = Field(default=30, gt=0)
    auth_refresh_cookie_name: str = "milly_refresh"
    auth_refresh_cookie_secure: bool = True
    auth_refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    admin_username: str = Field(default="admin", min_length=1)
    admin_password: SecretStr | None = None
    admin_session_max_age_seconds: int = Field(default=43_200, gt=0)

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
        Validate settings that are unsafe outside local development.

        Purpose:
            Prevent production deployments from starting with debug output,
            development credentials, or an insecure refresh cookie.

        Returns:
            Settings: The validated settings instance.

        Raises:
            ValueError: If production enables debug, lacks a real Telegram bot
                token or a strong JWT secret, configures a weak admin password,
                or configures an insecure refresh cookie.
        """
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        if self.app_env == "production":
            bot_token = (
                self.telegram_bot_token.get_secret_value().strip()
                if self.telegram_bot_token is not None
                else ""
            )
            if ":" not in bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN must be a real bot token in production")

            jwt_secret = (
                self.auth_jwt_secret.get_secret_value() if self.auth_jwt_secret is not None else ""
            )
            if len(jwt_secret.strip()) < 32 or "change-me" in jwt_secret.lower():
                raise ValueError("AUTH_JWT_SECRET must be a strong secret in production")
            if self.admin_password is not None:
                admin_password = self.admin_password.get_secret_value()
                if len(admin_password.strip()) < 16 or "change-me" in admin_password.lower():
                    raise ValueError("ADMIN_PASSWORD must be a strong password in production")
            if not self.auth_refresh_cookie_secure:
                raise ValueError("AUTH_REFRESH_COOKIE_SECURE must be true in production")
            if self.auth_refresh_cookie_samesite == "none":
                raise ValueError("AUTH_REFRESH_COOKIE_SAMESITE=none is not supported")
        return self

    @property
    def docs_enabled(self) -> bool:
        """Return whether OpenAPI/docs routes are enabled."""
        if self.enable_docs is not None:
            return self.enable_docs
        return self.app_env != "production"

    @property
    def admin_enabled(self) -> bool:
        """Return whether the admin panel has all required credentials."""
        if self.admin_password is None or self.auth_jwt_secret is None:
            return False
        return bool(self.admin_password.get_secret_value().strip()) and bool(
            self.auth_jwt_secret.get_secret_value()
        )


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
