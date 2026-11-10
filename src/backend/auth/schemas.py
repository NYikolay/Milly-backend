"""Compatibility re-exports of auth API request and response schemas."""

from backend.api.auth.schemas import AuthResponse as AuthResponse
from backend.api.auth.schemas import TelegramAuthRequest as TelegramAuthRequest
from backend.api.auth.schemas import TelegramProfileResponse as TelegramProfileResponse
from backend.api.auth.schemas import TimezoneUpdate as TimezoneUpdate
from backend.api.auth.schemas import TokenResponse as TokenResponse
from backend.api.auth.schemas import UserResponse as UserResponse

__all__ = [
    "AuthResponse",
    "TelegramAuthRequest",
    "TelegramProfileResponse",
    "TimezoneUpdate",
    "TokenResponse",
    "UserResponse",
]
