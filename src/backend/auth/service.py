"""Compatibility re-exports of the authentication application service."""

from backend.application.auth.service import AuthService as AuthService
from backend.domain.auth.models import LoginResult as LoginResult
from backend.domain.auth.models import TokenPair as TokenPair

__all__ = ["AuthService", "LoginResult", "TokenPair"]
