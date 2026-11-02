"""Public API error contract building blocks."""

from __future__ import annotations

from backend.api.errors.handlers import (
    handle_app_error,
    handle_http_exception,
    handle_response_validation_error,
    handle_unexpected_error,
    handle_validation_error,
    register_exception_handlers,
    render_error_response,
    resolve_request_id,
)
from backend.api.errors.mapping import (
    ErrorPayload,
    payload_from_app_error,
    payload_from_http_exception,
    payload_from_unexpected_error,
    payload_from_validation_error,
)
from backend.api.errors.schemas import (
    ErrorDetails,
    ErrorResponse,
    FieldError,
    ValidationCode,
)
from backend.api.errors.validation import normalize_validation_errors

__all__ = [
    "ErrorDetails",
    "ErrorPayload",
    "ErrorResponse",
    "FieldError",
    "ValidationCode",
    "handle_app_error",
    "handle_http_exception",
    "handle_response_validation_error",
    "handle_unexpected_error",
    "handle_validation_error",
    "normalize_validation_errors",
    "payload_from_app_error",
    "payload_from_http_exception",
    "payload_from_unexpected_error",
    "payload_from_validation_error",
    "register_exception_handlers",
    "render_error_response",
    "resolve_request_id",
]
