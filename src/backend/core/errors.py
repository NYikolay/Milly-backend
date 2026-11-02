from __future__ import annotations

from collections.abc import Mapping

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api.schemas import ErrorDetail, ErrorResponse
from backend.core.config import Settings
from backend.core.middleware import SECURITY_HEADERS

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base error for expected application failures."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: object | None = None,
    ) -> None:
        """
        Initialize an expected application error.

        Purpose:
            Carry the HTTP status and machine-readable code used by the unified
            error envelope.

        Args:
            status_code: HTTP status code returned to the client.
            code: Stable machine-readable error code.
            message: Human-readable error message.
            details: Optional JSON-serializable error details.

        Notes:
            - The message is also passed to ``Exception`` for standard
              exception reporting.
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class NotFoundError(AppError):
    """Raised when a requested resource does not exist."""

    def __init__(
        self,
        message: str = "Not found",
        details: object | None = None,
    ) -> None:
        """
        Initialize a not-found error with its default status and code.

        Purpose:
            Provide the standard 404 ``not_found`` error for business code.

        Args:
            message: Human-readable error message.
            details: Optional JSON-serializable error details.
        """
        super().__init__(
            status_code=404,
            code="not_found",
            message=message,
            details=details,
        )


class ConflictError(AppError):
    """Raised when an operation conflicts with current state."""

    def __init__(
        self,
        message: str = "Conflict",
        details: object | None = None,
    ) -> None:
        """
        Initialize a conflict error with its default status and code.

        Purpose:
            Provide the standard 409 ``conflict`` error for business code.

        Args:
            message: Human-readable error message.
            details: Optional JSON-serializable error details.
        """
        super().__init__(
            status_code=409,
            code="conflict",
            message=message,
            details=details,
        )


def _request_id(request: Request) -> str | None:
    """
    Read the current request ID from request state or logging context.

    Purpose:
        Supply the request ID for error envelopes even when the request context
        middleware has already cleared its context variables.

    Args:
        request: Request being handled.

    Returns:
        str | None: The request ID when one is available.
    """
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str):
        return request_id
    contextual_request_id = structlog.contextvars.get_contextvars().get("request_id")
    return contextual_request_id if isinstance(contextual_request_id, str) else None


def _cors_headers(request: Request, settings: Settings | None) -> dict[str, str]:
    """
    Build CORS headers for a response that bypasses ``CORSMiddleware``.

    Purpose:
        Keep unhandled 500 responses readable by browser clients when the
        request origin is allowed by the application settings.

    Args:
        request: Request being handled.
        settings: Application settings, or ``None`` when unavailable.

    Returns:
        dict[str, str]: CORS headers for the explicit request origin, or an
            empty mapping when the request has no allowed origin.

    Notes:
        - The application configures credentials, so an allowed wildcard
          origin is echoed explicitly, matching ``CORSMiddleware`` behavior.
        - The returned headers are meant to be merged into an error response.
    """
    if not isinstance(settings, Settings):
        return {}
    origin = request.headers.get("origin")
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


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: object | None,
    request_id: str | None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """
    Build a response with the unified error envelope.

    Purpose:
        Serialize one error payload consistently for every exception handler.

    Args:
        status_code: HTTP status code of the response.
        code: Stable machine-readable error code.
        message: Human-readable error message.
        details: Optional error details; encoded to JSON-safe values.
        request_id: Request ID associated with the failure.
        headers: Optional extra response headers.

    Returns:
        JSONResponse: Response containing the error envelope.

    Notes:
        - Every envelope carries the shared security headers because unhandled
          500 responses bypass ``SecurityHeadersMiddleware``.
        - ``X-Request-ID`` is added to the response headers when known, which
          also covers responses produced outside the request context
          middleware.
    """
    response_headers = dict(SECURITY_HEADERS)
    response_headers.update(headers or {})
    if request_id is not None:
        response_headers.setdefault("X-Request-ID", request_id)
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=jsonable_encoder(details),
            request_id=request_id,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(),
        headers=response_headers,
    )


async def _handle_app_error(request: Request, exc: Exception) -> Response:
    """
    Render an expected application error.

    Purpose:
        Convert ``AppError`` instances into the unified error envelope using
        their status, code, and details.

    Args:
        request: Request being handled.
        exc: Expected application error.

    Returns:
        Response: JSON response with the error envelope.
    """
    assert isinstance(exc, AppError)
    return _error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request_id=_request_id(request),
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    """
    Render a request validation failure.

    Purpose:
        Convert ``RequestValidationError`` into a 422 envelope with JSON-safe
        Pydantic error details.

    Args:
        request: Request being handled.
        exc: Validation error raised while parsing the request.

    Returns:
        Response: JSON response with the error envelope.
    """
    assert isinstance(exc, RequestValidationError)
    return _error_response(
        status_code=422,
        code="validation_error",
        message="Validation failed",
        details=exc.errors(),
        request_id=_request_id(request),
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    """
    Render an HTTP exception with a status-derived code.

    Purpose:
        Convert Starlette and FastAPI HTTP exceptions into the unified error
        envelope.

    Args:
        request: Request being handled.
        exc: HTTP exception raised by the application or router.

    Returns:
        Response: JSON response with the error envelope.
    """
    assert isinstance(exc, StarletteHTTPException)
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return _error_response(
        status_code=exc.status_code,
        code=f"http_{exc.status_code}",
        message=message,
        details=None,
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """
    Render an unexpected exception and log its traceback.

    Purpose:
        Hide implementation details in production while retaining full
        diagnostics in debug mode and the server log.

    Args:
        request: Request being handled.
        exc: Unexpected exception that escaped the application.

    Returns:
        Response: JSON response with the unified error envelope.

    Notes:
        - Details are included only when the application settings enable
          ``debug``.
        - Security and CORS headers are added here because the response is
          produced by Starlette's outer ``ServerErrorMiddleware`` and never
          reaches ``SecurityHeadersMiddleware`` or ``CORSMiddleware``.
        - The log record includes the request ID, method, and path because the
          request context variables have already been cleared.
    """
    settings = getattr(request.app.state, "settings", None)
    debug = settings.debug if isinstance(settings, Settings) else False
    logger.exception(
        "http.request.failed",
        exc_info=exc,
        request_id=_request_id(request),
        method=request.method,
        path=request.url.path,
    )
    details = {"exception": type(exc).__name__, "message": str(exc)} if debug else None
    return _error_response(
        status_code=500,
        code="internal_error",
        message="Internal server error",
        details=details,
        request_id=_request_id(request),
        headers=_cors_headers(request, settings if isinstance(settings, Settings) else None),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register handlers that produce the unified error envelope.

    Purpose:
        Install handlers for expected application errors, validation failures,
        HTTP exceptions, and unexpected exceptions.

    Args:
        app: FastAPI application receiving the handlers.

    Returns:
        None: This function only registers handlers.

    Notes:
        - Handler registration replaces FastAPI's default HTTP and validation
          handlers.
    """
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)
