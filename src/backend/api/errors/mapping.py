from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api.errors.schemas import ValidationCode
from backend.api.errors.validation import normalize_validation_errors
from backend.core.errors import AppError, ErrorCode, get_error_spec

_HTTP_ERROR_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    501: ErrorCode.NOT_IMPLEMENTED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


@dataclass(frozen=True)
class ErrorPayload:
    """Transport-independent error data ready for response rendering."""

    status_code: int
    code: ErrorCode
    message: str
    description: str
    log_level: int
    details: Mapping[str, object] | None = None
    headers: Mapping[str, str] | None = None


def payload_from_app_error(exc: AppError) -> ErrorPayload:
    """
    Build an error payload from an application error.

    Purpose:
        Use the error's registry-backed status while preserving per-instance
        message, description, log level, details, and headers.

    Args:
        exc: Expected application failure.

    Returns:
        ErrorPayload: Payload describing the failure.
    """
    spec = get_error_spec(exc.code)
    return ErrorPayload(
        status_code=spec.status_code,
        code=exc.code,
        message=exc.message,
        description=exc.description,
        log_level=exc.log_level,
        details=exc.details,
        headers=exc.headers,
    )


def payload_from_http_exception(exc: StarletteHTTPException) -> ErrorPayload:
    """
    Build an error payload from an HTTP exception.

    Purpose:
        Map HTTP statuses to stable error codes while exposing client-error
        details and hiding server-error details.

    Args:
        exc: HTTP exception raised by the application or router.

    Returns:
        ErrorPayload: Payload describing the HTTP failure.

    Notes:
        - A string detail of a 4xx response becomes the technical message.
        - A non-string detail of a 4xx response is exposed as structured
          details.
        - Details of 5xx responses are always ignored.
        - Exception headers are preserved for every status.
    """
    code = _HTTP_ERROR_CODES.get(exc.status_code, ErrorCode.HTTP_ERROR)
    spec = get_error_spec(code)
    message = spec.message
    details: Mapping[str, object] | None = None
    if 400 <= exc.status_code < 500 and exc.detail is not None:
        if isinstance(exc.detail, str):
            message = exc.detail
        else:
            details = {"detail": exc.detail}
    return ErrorPayload(
        status_code=exc.status_code,
        code=code,
        message=message,
        description=spec.description,
        log_level=spec.log_level,
        details=details,
        headers=exc.headers,
    )


def payload_from_validation_error(exc: RequestValidationError) -> ErrorPayload:
    """
    Build an error payload from a request validation failure.

    Purpose:
        Normalize request validation errors into stable field details and
        classify malformed JSON as a bad request.

    Args:
        exc: Request validation error raised while parsing the request.

    Returns:
        ErrorPayload: Payload with normalized field errors as details.

    Notes:
        - Any invalid JSON error maps the whole response to HTTP 400.
        - Otherwise validation failures map to HTTP 422.
    """
    fields = normalize_validation_errors(exc)
    has_invalid_json = any(field.code == ValidationCode.INVALID_JSON for field in fields)
    code = ErrorCode.BAD_REQUEST if has_invalid_json else ErrorCode.VALIDATION_ERROR
    spec = get_error_spec(code)
    return ErrorPayload(
        status_code=spec.status_code,
        code=code,
        message=spec.message,
        description=spec.description,
        log_level=spec.log_level,
        details={"fields": [field.model_dump() for field in fields]},
    )


def payload_from_unexpected_error(exc: Exception, *, debug: bool) -> ErrorPayload:
    """
    Build an error payload from an unexpected exception.

    Purpose:
        Hide unexpected failure details outside debug mode while retaining
        enough diagnostics for local development.

    Args:
        exc: Unexpected exception that escaped the application.
        debug: Whether debug details may be exposed.

    Returns:
        ErrorPayload: Internal-error payload, with debug details when enabled.

    Notes:
        - Details are omitted unless `debug` is true.
    """
    spec = get_error_spec(ErrorCode.INTERNAL_ERROR)
    details: Mapping[str, object] | None = None
    if debug:
        details = {"exception": type(exc).__name__, "message": str(exc)}
    return ErrorPayload(
        status_code=spec.status_code,
        code=ErrorCode.INTERNAL_ERROR,
        message=spec.message,
        description=spec.description,
        log_level=spec.log_level,
        details=details,
    )
