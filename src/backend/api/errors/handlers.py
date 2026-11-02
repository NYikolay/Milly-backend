from __future__ import annotations

from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from backend.api.errors.mapping import (
    ErrorPayload,
    payload_from_app_error,
    payload_from_http_exception,
    payload_from_unexpected_error,
    payload_from_validation_error,
)
from backend.api.errors.schemas import ErrorResponse
from backend.core.config import Settings, get_settings
from backend.core.errors import AppError
from backend.core.middleware import SECURITY_HEADERS

logger = structlog.get_logger(__name__)


def resolve_request_id(scope: Scope) -> str:
    """
    Resolve the request ID for an error response.

    Purpose:
        Reuse the request ID stored by the request context middleware, then the
        bound structlog context, and generate a fresh ID as a last resort.

    Args:
        scope: ASGI scope of the failing request.

    Returns:
        str: The request ID associated with the failure.

    Notes:
        - A generated ID is used when the error occurs outside the request
          context middleware.
    """
    state = scope.get("state") or {}
    request_id = state.get("request_id")
    if isinstance(request_id, str):
        return request_id
    contextual_request_id = structlog.contextvars.get_contextvars().get("request_id")
    if isinstance(contextual_request_id, str):
        return contextual_request_id
    return uuid4().hex


def _resolve_settings(request: Request) -> Settings:
    """
    Resolve application settings for an exception handler.

    Purpose:
        Read the settings attached to the application and fall back to
        environment-backed settings when the application state lacks them.

    Args:
        request: Request being handled.

    Returns:
        Settings: Application settings used by the error renderer.
    """
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return get_settings()


def _cors_headers(scope: Scope, settings: Settings) -> dict[str, str]:
    """
    Build CORS headers for an error response.

    Purpose:
        Keep error responses readable by browser clients when the request
        origin is allowed by the application settings.

    Args:
        scope: ASGI scope of the failing request.
        settings: Application settings containing the allowed origins.

    Returns:
        dict[str, str]: CORS headers for the explicit request origin, or an
            empty mapping when the origin is absent or disallowed.

    Notes:
        - The application configures credentials, so an allowed wildcard origin
          is echoed explicitly, matching ``CORSMiddleware`` behavior.
    """
    origin = Headers(scope=scope).get("origin")
    if origin is None:
        return {}
    allowed = "*" in settings.cors_origins or origin in settings.cors_origins
    if not allowed:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Expose-Headers": "X-Request-ID",
        "Vary": "Origin",
    }


def render_error_response(
    payload: ErrorPayload,
    *,
    scope: Scope,
    settings: Settings,
    exc: Exception | None = None,
) -> JSONResponse:
    """
    Render a transport independent error payload as a JSON response.

    Purpose:
        Build the flat error envelope, attach security, error, and CORS
        headers, and emit one structured error log record.

    Args:
        payload: Error data produced by a payload mapper.
        scope: ASGI scope of the failing request.
        settings: Application settings containing the allowed CORS origins.
        exc: Original exception, used for server-error tracebacks.

    Returns:
        JSONResponse: Response containing the flat error envelope.

    Notes:
        - Details that cannot be encoded or validated are dropped and logged as
          ``api.error_details_dropped``.
        - Response headers include the shared security headers, payload
          headers, ``X-Request-ID``, and explicit CORS headers for the request
          origin.
        - Client errors use the payload log level; server errors are logged at
          error level with the original exception traceback.
        - Error details are never written to the log.
    """
    request_id = resolve_request_id(scope)
    try:
        content = ErrorResponse(
            code=payload.code,
            message=payload.message,
            description=payload.description,
            details=jsonable_encoder(payload.details),
            request_id=request_id,
        ).model_dump(exclude_none=True)
    except Exception:
        logger.warning(
            "api.error_details_dropped",
            error_code=payload.code.value,
            status_code=payload.status_code,
            request_id=request_id,
        )
        content = ErrorResponse(
            code=payload.code,
            message=payload.message,
            description=payload.description,
            request_id=request_id,
        ).model_dump(exclude_none=True)

    headers = dict(SECURITY_HEADERS)
    if payload.headers:
        headers.update(payload.headers)
    headers.setdefault("X-Request-ID", request_id)
    headers.update(_cors_headers(scope, settings))

    log_fields: dict[str, object] = {
        "error_code": payload.code.value,
        "status_code": payload.status_code,
        "request_id": request_id,
        "method": scope.get("method", ""),
        "path": scope.get("path", ""),
    }
    if payload.status_code >= 500:
        logger.error("api.error", exc_info=exc, **log_fields)
    else:
        logger.log(payload.log_level, "api.error", **log_fields)

    return JSONResponse(status_code=payload.status_code, content=content, headers=headers)


async def handle_app_error(request: Request, exc: Exception) -> Response:
    """
    Render an expected application error.

    Purpose:
        Convert ``AppError`` instances into the flat error envelope.

    Args:
        request: Request being handled.
        exc: Expected application failure.

    Returns:
        Response: JSON response with the error envelope.
    """
    assert isinstance(exc, AppError)
    return render_error_response(
        payload_from_app_error(exc),
        scope=request.scope,
        settings=_resolve_settings(request),
        exc=exc,
    )


async def handle_validation_error(request: Request, exc: Exception) -> Response:
    """
    Render a request validation failure.

    Purpose:
        Convert ``RequestValidationError`` instances into the flat error
        envelope with normalized field details.

    Args:
        request: Request being handled.
        exc: Validation error raised while parsing the request.

    Returns:
        Response: JSON response with the error envelope.
    """
    assert isinstance(exc, RequestValidationError)
    return render_error_response(
        payload_from_validation_error(exc),
        scope=request.scope,
        settings=_resolve_settings(request),
        exc=exc,
    )


async def handle_http_exception(request: Request, exc: Exception) -> Response:
    """
    Render an HTTP exception with a status-derived error code.

    Purpose:
        Convert Starlette and FastAPI HTTP exceptions into the flat error
        envelope.

    Args:
        request: Request being handled.
        exc: HTTP exception raised by the application or router.

    Returns:
        Response: JSON response with the error envelope.
    """
    assert isinstance(exc, StarletteHTTPException)
    return render_error_response(
        payload_from_http_exception(exc),
        scope=request.scope,
        settings=_resolve_settings(request),
        exc=exc,
    )


async def handle_response_validation_error(request: Request, exc: Exception) -> Response:
    """
    Render a response validation failure.

    Purpose:
        Treat a response that violates its declared model as an internal server
        error instead of leaking framework details.

    Args:
        request: Request being handled.
        exc: Response validation error raised while serializing the response.

    Returns:
        Response: JSON response with the internal error envelope.
    """
    assert isinstance(exc, ResponseValidationError)
    settings = _resolve_settings(request)
    return render_error_response(
        payload_from_unexpected_error(exc, debug=settings.debug),
        scope=request.scope,
        settings=settings,
        exc=exc,
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """
    Render an unexpected exception and log its traceback.

    Purpose:
        Hide implementation details in production while retaining full
        diagnostics in debug mode and the server log.

    Args:
        request: Request being handled.
        exc: Unexpected exception that escaped the application.

    Returns:
        Response: JSON response with the internal error envelope.

    Notes:
        - Details are included only when the application settings enable
          ``debug``.
        - Security and CORS headers are added by the renderer because the
          response is produced by Starlette's outer ``ServerErrorMiddleware``
          and never reaches the security headers middleware.
    """
    assert isinstance(exc, Exception)
    settings = _resolve_settings(request)
    return render_error_response(
        payload_from_unexpected_error(exc, debug=settings.debug),
        scope=request.scope,
        settings=settings,
        exc=exc,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register handlers that produce the flat error envelope.

    Purpose:
        Install handlers for expected application errors, validation failures,
        response validation failures, HTTP exceptions, and unexpected
        exceptions.

    Args:
        app: FastAPI application receiving the handlers.

    Returns:
        None: This function only registers handlers.

    Notes:
        - Handler registration replaces FastAPI's default HTTP and validation
          handlers.
    """
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(ResponseValidationError, handle_response_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
