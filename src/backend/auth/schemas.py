from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TelegramAuthRequest(BaseModel):
    """Telegram Mini App login request carrying raw init data."""

    init_data: str = Field(min_length=1, max_length=8192)


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
    timezone: str
    created_at: datetime
    telegram_profile: TelegramProfileResponse


class TimezoneUpdate(BaseModel):
    """Validated profile timezone update request."""

    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """
        Require a valid IANA timezone name.

        Purpose:
            Reject unknown zones before the service persists a profile update.

        Args:
            value: Candidate IANA timezone name.

        Returns:
            str: The unchanged valid name.

        Raises:
            ValueError: If the name cannot be resolved by ``ZoneInfo``.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Invalid timezone") from error
        return value


class TokenResponse(BaseModel):
    """Access token issued after login or refresh."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class AuthResponse(TokenResponse):
    """Login response containing the access token and authenticated user."""

    user: UserResponse
