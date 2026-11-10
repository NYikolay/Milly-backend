"""Compatibility re-exports of authentication errors."""

from backend.application.auth.errors import (
    AuthNotConfiguredError as AuthNotConfiguredError,
)
from backend.application.auth.errors import (
    RefreshTokenGraceError as RefreshTokenGraceError,
)

__all__ = ["AuthNotConfiguredError", "RefreshTokenGraceError"]
