from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TelegramAuthRequest(BaseModel):
    """Telegram Mini App login request carrying raw init data."""

    init_data: str = Field(min_length=1)


class TelegramProfileResponse(BaseModel):
    """Public Telegram identity fields returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    telegram_id: int
    is_bot: bool | None = None
    first_name: str
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool
    added_to_attachment_menu: bool | None = None
    allows_write_to_pm: bool | None = None
    photo_url: str | None = None


class UserResponse(BaseModel):
    """Application user together with the linked Telegram profile."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    telegram_profile: TelegramProfileResponse


class TokenResponse(BaseModel):
    """Access token issued after login or refresh."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class AuthResponse(TokenResponse):
    """Login response containing the access token and authenticated user."""

    user: UserResponse
