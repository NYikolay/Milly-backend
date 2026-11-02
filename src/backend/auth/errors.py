from __future__ import annotations

from backend.core.errors import ErrorCode, SemanticAppError


class AuthNotConfiguredError(SemanticAppError):
    """Raised when required authentication settings are missing."""

    _code = ErrorCode.AUTH_NOT_CONFIGURED
