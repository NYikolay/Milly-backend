from __future__ import annotations

from fastapi import Request, Response, status

from backend.api.auth.cookies import (
    clear_refresh_cookie,
    set_refresh_cookie,
    validate_auth_origin,
)
from backend.api.auth.schemas import (
    AuthResponse,
    TelegramAuthRequest,
    TimezoneUpdate,
    TokenResponse,
    UserResponse,
)
from backend.api.deps import AuthServiceDep, CurrentUserDep
from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.mappers.auth import auth_response, token_response, user_response
from backend.core.config import Settings
from backend.core.errors import ErrorCode, UnauthorizedError

router = StandardizedAPIRouter(prefix="/auth", tags=["auth"])

_ORIGIN_FORBIDDEN = "The `Origin` header is neither the API origin nor an allowed CORS origin."


@router.post(
    "/telegram",
    response_model=AuthResponse,
    responses=document_errors(
        {
            ErrorCode.FORBIDDEN: _ORIGIN_FORBIDDEN,
            ErrorCode.AUTH_NOT_CONFIGURED: (
                "The Telegram bot token or JWT secret is not configured."
            ),
            ErrorCode.UNAUTHORIZED: (
                "The Telegram init data is invalid, stale, or belongs to a bot."
            ),
        }
    ),
)
async def telegram_login(
    payload: TelegramAuthRequest,
    request: Request,
    response: Response,
    service: AuthServiceDep,
) -> AuthResponse:
    """
    Authenticate a Telegram Mini App launch and issue tokens.

    Purpose:
        Validate signed Telegram init data, persist or refresh the local
        profile, return a short-lived access token, and store the refresh token
        in an HttpOnly cookie.

    Args:
        payload: Raw Telegram ``initData`` query string.
        request: Incoming request carrying the application settings.
        response: Response used to attach the refresh cookie.
        service: Authentication service performing login.

    Returns:
        AuthResponse: Access token, token metadata, and the authenticated user
            with the Telegram profile.

    Raises:
        ForbiddenError (HTTP 403): If the request origin is not allowed.
        AuthNotConfiguredError (HTTP 503): If authentication settings are
            missing.
        UnauthorizedError (HTTP 401): If the init data is invalid, stale, or
            belongs to a bot.

    Notes:
        - The refresh token is returned only as an HttpOnly cookie and never in
          the response body.
        - Raw init data and tokens are never logged.
    """
    settings: Settings = request.app.state.settings
    validate_auth_origin(request, settings)
    result = await service.login(payload.init_data)
    set_refresh_cookie(response, result.tokens.refresh_token, settings)
    return auth_response(result, settings.auth_access_ttl_seconds)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses=document_errors(
        {
            ErrorCode.FORBIDDEN: _ORIGIN_FORBIDDEN,
            ErrorCode.UNAUTHORIZED: (
                "The refresh cookie is missing, unknown, expired, revoked, or reused."
            ),
            ErrorCode.AUTH_NOT_CONFIGURED: "The JWT secret is not configured.",
        }
    ),
)
async def refresh_tokens(
    request: Request,
    response: Response,
    service: AuthServiceDep,
) -> TokenResponse:
    """
    Rotate the refresh cookie and issue a new access token.

    Purpose:
        Read the opaque refresh token from the HttpOnly cookie, rotate the
        server-side session, replace the cookie, and return only the new access
        token.

    Args:
        request: Incoming request carrying the application settings and cookie.
        response: Response used to attach the replacement refresh cookie.
        service: Authentication service performing refresh rotation.

    Returns:
        TokenResponse: Newly issued access token and token metadata.

    Raises:
        ForbiddenError (HTTP 403): If the request origin is not allowed.
        UnauthorizedError (HTTP 401): If the refresh cookie is missing,
            unknown, expired, revoked, or reused.
        RefreshTokenGraceError (HTTP 401): If a just-rotated token is reused
            within the grace window; this is an ``UnauthorizedError`` subclass.
        AuthNotConfiguredError (HTTP 503): If authentication settings are
            missing.

    Notes:
        - Refresh errors propagate before the cookie is modified, so a failed
          refresh never clears the existing cookie.
        - Raw refresh tokens are never logged.
    """
    settings: Settings = request.app.state.settings
    validate_auth_origin(request, settings)
    raw_refresh_token = request.cookies.get(settings.auth_refresh_cookie_name)
    if raw_refresh_token is None:
        raise UnauthorizedError("Refresh token is missing")
    tokens = await service.refresh(raw_refresh_token)
    set_refresh_cookie(response, tokens.refresh_token, settings)
    return token_response(tokens, settings.auth_access_ttl_seconds)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=document_errors({ErrorCode.FORBIDDEN: _ORIGIN_FORBIDDEN}),
)
async def logout(
    request: Request,
    response: Response,
    service: AuthServiceDep,
) -> Response:
    """
    Revoke the refresh-token family and clear the refresh cookie.

    Purpose:
        Terminate every rotation derived from the presented refresh token and
        expire the client cookie even when the token is absent or unknown.

    Args:
        request: Incoming request carrying the application settings and cookie.
        response: Response used to expire the refresh cookie.
        service: Authentication service performing logout.

    Returns:
        Response: Empty 204 response with the expiry cookie.

    Raises:
        ForbiddenError (HTTP 403): If the request origin is not allowed.

    Notes:
        - Logout is idempotent and does not reveal whether the cookie existed.
        - Raw refresh tokens are never logged.
    """
    settings: Settings = request.app.state.settings
    validate_auth_origin(request, settings)
    raw_refresh_token = request.cookies.get(settings.auth_refresh_cookie_name)
    await service.logout(raw_refresh_token)
    clear_refresh_cookie(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/me",
    response_model=UserResponse,
    responses=document_errors(
        {
            ErrorCode.AUTH_NOT_CONFIGURED: "The JWT secret is not configured.",
            ErrorCode.UNAUTHORIZED: (
                "The bearer token is missing or invalid, the user no longer "
                "exists, or the user has no Telegram profile."
            ),
        }
    ),
)
async def read_current_user(user: CurrentUserDep) -> UserResponse:
    """
    Return the authenticated user with the Telegram profile.

    Purpose:
        Expose the principal resolved from a verified access token for client
        session bootstrap and profile display.

    Args:
        user: Current user with ``telegram_profile`` loaded.

    Returns:
        UserResponse: Public user fields and the Telegram profile.

    Raises:
        AuthNotConfiguredError (HTTP 503): If no JWT secret is configured.
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid, the user no longer exists, or the user has no Telegram
            profile.
    """
    return user_response(user)


@router.patch(
    "/me/timezone",
    response_model=UserResponse,
    responses=document_errors(
        {
            ErrorCode.AUTH_NOT_CONFIGURED: "The JWT secret is not configured.",
            ErrorCode.BAD_REQUEST: "The stored or requested timezone is invalid.",
            ErrorCode.UNAUTHORIZED: (
                "The bearer token is missing or invalid, the user no longer "
                "exists, or the user has no Telegram profile."
            ),
        }
    ),
)
async def update_timezone(
    payload: TimezoneUpdate,
    user: CurrentUserDep,
    service: AuthServiceDep,
) -> UserResponse:
    """
    Update the authenticated user's IANA timezone.

    Purpose:
        Persist the timezone used for local task days and scheduled reminder
        slots.

    Args:
        payload: Validated timezone update request.
        user: Current user with ``telegram_profile`` loaded.
        service: Authentication service performing the update.

    Returns:
        UserResponse: The updated public user fields and Telegram profile.

    Raises:
        AuthNotConfiguredError (HTTP 503): If no JWT secret is configured.
        BadRequestError (HTTP 400): If the timezone is invalid at the service
            boundary.
        UnauthorizedError (HTTP 401): If the bearer token is missing or
            invalid, or the user no longer exists.

    Notes:
        - The request-scoped session commits after the response is built.
    """
    updated = await service.update_timezone(user.id, payload.timezone)
    return user_response(updated)
