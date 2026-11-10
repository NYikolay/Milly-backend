"""Mappers from authentication domain DTOs to API response models."""

from __future__ import annotations

from backend.api.auth.schemas import (
    AuthResponse,
    TelegramProfileResponse,
    TokenResponse,
    UserResponse,
)
from backend.domain.auth.models import LoginResult, TokenPair
from backend.domain.entities import TelegramProfile, User


def profile_response(profile: TelegramProfile) -> TelegramProfileResponse:
    """
    Map a domain Telegram profile to its API response.

    Purpose:
        Serialize the public Telegram identity fields returned on login and
        profile reads without depending on ORM rows.

    Args:
        profile: Domain Telegram profile linked to the authenticated user.

    Returns:
        TelegramProfileResponse: Public Telegram identity fields.
    """
    return TelegramProfileResponse(
        telegram_id=profile.telegram_id,
        is_bot=profile.is_bot,
        first_name=profile.first_name,
        last_name=profile.last_name,
        username=profile.username,
        language_code=profile.language_code,
        is_premium=profile.is_premium,
        added_to_attachment_menu=profile.added_to_attachment_menu,
        allows_write_to_pm=profile.allows_write_to_pm,
        photo_url=profile.photo_url,
    )


def user_response(user: User) -> UserResponse:
    """
    Map a domain user to its API response.

    Purpose:
        Serialize the authenticated user together with the loaded Telegram
        profile for login and profile routes.

    Args:
        user: Domain user with ``telegram_profile`` loaded.

    Returns:
        UserResponse: Public user fields and the Telegram profile.

    Raises:
        ValueError: If ``user.telegram_profile`` is not loaded.
    """
    profile = user.telegram_profile
    if profile is None:
        raise ValueError("User has no Telegram profile")
    return UserResponse(
        id=user.id,
        timezone=user.timezone,
        created_at=user.created_at,
        telegram_profile=profile_response(profile),
    )


def auth_response(result: LoginResult, expires_in: int) -> AuthResponse:
    """
    Map a successful login result to its API response.

    Purpose:
        Combine the issued access token with the authenticated user and
        Telegram profile returned by the login route.

    Args:
        result: Login result returned by the authentication service.
        expires_in: Access-token lifetime in seconds.

    Returns:
        AuthResponse: Access token metadata and the authenticated user.
    """
    return AuthResponse(
        access_token=result.tokens.access_token,
        expires_in=expires_in,
        user=UserResponse(
            id=result.user.id,
            timezone=result.user.timezone,
            created_at=result.user.created_at,
            telegram_profile=profile_response(result.profile),
        ),
    )


def token_response(tokens: TokenPair, expires_in: int) -> TokenResponse:
    """
    Map an issued token pair to the refresh response.

    Purpose:
        Return only the newly issued access token and its metadata; the refresh
        token is delivered through the HTTP-only cookie.

    Args:
        tokens: Issued access and refresh token pair.
        expires_in: Access-token lifetime in seconds.

    Returns:
        TokenResponse: Access token and token metadata.
    """
    return TokenResponse(access_token=tokens.access_token, expires_in=expires_in)
