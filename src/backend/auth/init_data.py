"""Compatibility re-exports of Telegram init-data validation."""

from backend.domain.auth.models import TelegramUser as TelegramUser
from backend.infrastructure.auth.init_data import (
    validate_init_data as validate_init_data,
)

__all__ = ["TelegramUser", "validate_init_data"]
