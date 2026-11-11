"""HTTP status mapping for transport-independent error codes."""

from __future__ import annotations

from backend.core.errors import ErrorCode

ERROR_STATUS_CODES: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.AUTH_NOT_CONFIGURED: 503,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.NOT_IMPLEMENTED: 501,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.DATABASE_UNAVAILABLE: 503,
    ErrorCode.TASK_ENQUEUE_FAILED: 503,
    ErrorCode.HTTP_ERROR: 500,
}


def status_code_for(code: ErrorCode) -> int:
    """
    Return the HTTP status code used to render an error code.

    Purpose:
        Keep the transport mapping in the API layer while letting runtime
        mappers and OpenAPI documentation resolve it from one source.

    Args:
        code: Error code whose HTTP status is requested.

    Returns:
        int: HTTP status code registered for `code`.

    Raises:
        KeyError: If `code` has no registered HTTP status.
    """
    return ERROR_STATUS_CODES[code]
