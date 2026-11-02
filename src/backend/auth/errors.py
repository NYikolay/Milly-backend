from __future__ import annotations

from backend.core.errors import ErrorCode, SemanticAppError, UnauthorizedError


class AuthNotConfiguredError(SemanticAppError):
    """Raised when required authentication settings are missing."""

    _code = ErrorCode.AUTH_NOT_CONFIGURED


class RefreshTokenGraceError(UnauthorizedError):
    """Raised when a just-rotated refresh token is reused within the grace window."""
