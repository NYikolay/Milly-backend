"""Compatibility re-exports of supported date and instant bounds."""

from backend.domain.task_management.policies import (
    MAX_SUPPORTED_DATE,
    MAX_SUPPORTED_DATETIME,
    MIN_SUPPORTED_DATE,
    MIN_SUPPORTED_DATETIME,
    require_supported_date,
    require_supported_datetime,
)

__all__ = [
    "MAX_SUPPORTED_DATE",
    "MAX_SUPPORTED_DATETIME",
    "MIN_SUPPORTED_DATE",
    "MIN_SUPPORTED_DATETIME",
    "require_supported_date",
    "require_supported_datetime",
]
