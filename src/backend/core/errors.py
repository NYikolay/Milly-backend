from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar


class ErrorCode(StrEnum):
    """Stable machine-readable error codes returned by the API."""

    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    AUTH_NOT_CONFIGURED = "auth_not_configured"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    NOT_IMPLEMENTED = "not_implemented"
    SERVICE_UNAVAILABLE = "service_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    TASK_ENQUEUE_FAILED = "task_enqueue_failed"
    HTTP_ERROR = "http_error"


@dataclass(frozen=True)
class ErrorSpec:
    """Registry entry describing the transport-independent behavior of an error code."""

    status_code: int
    log_level: int
    message: str
    description: str


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.BAD_REQUEST: ErrorSpec(
        status_code=400,
        log_level=logging.WARNING,
        message="Bad request",
        description="Некорректный запрос",
    ),
    ErrorCode.VALIDATION_ERROR: ErrorSpec(
        status_code=422,
        log_level=logging.INFO,
        message="Request validation failed",
        description="Проверьте правильность заполнения полей",
    ),
    ErrorCode.UNAUTHORIZED: ErrorSpec(
        status_code=401,
        log_level=logging.WARNING,
        message="Authentication required",
        description="Требуется авторизация",
    ),
    ErrorCode.FORBIDDEN: ErrorSpec(
        status_code=403,
        log_level=logging.WARNING,
        message="Permission denied",
        description="Недостаточно прав",
    ),
    ErrorCode.AUTH_NOT_CONFIGURED: ErrorSpec(
        status_code=503,
        log_level=logging.ERROR,
        message="Authentication is not configured",
        description="Аутентификация не настроена",
    ),
    ErrorCode.NOT_FOUND: ErrorSpec(
        status_code=404,
        log_level=logging.INFO,
        message="Resource not found",
        description="Запрошенный ресурс не найден",
    ),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorSpec(
        status_code=405,
        log_level=logging.WARNING,
        message="Method not allowed",
        description="Метод не поддерживается",
    ),
    ErrorCode.CONFLICT: ErrorSpec(
        status_code=409,
        log_level=logging.WARNING,
        message="Conflict",
        description="Операция конфликтует с текущим состоянием",
    ),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorSpec(
        status_code=413,
        log_level=logging.WARNING,
        message="Payload too large",
        description="Слишком большой запрос",
    ),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: ErrorSpec(
        status_code=415,
        log_level=logging.WARNING,
        message="Unsupported media type",
        description="Неподдерживаемый тип содержимого",
    ),
    ErrorCode.RATE_LIMITED: ErrorSpec(
        status_code=429,
        log_level=logging.WARNING,
        message="Too many requests",
        description="Слишком много запросов",
    ),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(
        status_code=500,
        log_level=logging.ERROR,
        message="Internal server error",
        description="Внутренняя ошибка сервера",
    ),
    ErrorCode.NOT_IMPLEMENTED: ErrorSpec(
        status_code=501,
        log_level=logging.WARNING,
        message="Not implemented",
        description="Функция не реализована",
    ),
    ErrorCode.SERVICE_UNAVAILABLE: ErrorSpec(
        status_code=503,
        log_level=logging.ERROR,
        message="Service unavailable",
        description="Сервис временно недоступен",
    ),
    ErrorCode.DATABASE_UNAVAILABLE: ErrorSpec(
        status_code=503,
        log_level=logging.ERROR,
        message="Database is unavailable",
        description="База данных временно недоступна",
    ),
    ErrorCode.TASK_ENQUEUE_FAILED: ErrorSpec(
        status_code=503,
        log_level=logging.ERROR,
        message="Failed to enqueue task",
        description="Не удалось поставить задачу в очередь",
    ),
    ErrorCode.HTTP_ERROR: ErrorSpec(
        status_code=500,
        log_level=logging.WARNING,
        message="HTTP error",
        description="Ошибка запроса",
    ),
}


def get_error_spec(code: ErrorCode) -> ErrorSpec:
    """
    Return the registered specification for an error code.

    Purpose:
        Centralize access to the status, logging level, and default texts of an
        error code used by exceptions and transport mappers.

    Args:
        code: Error code whose specification is requested.

    Returns:
        ErrorSpec: The registered specification for `code`.

    Raises:
        KeyError: If `code` has no registered specification.
    """
    return ERROR_SPECS[code]


class AppError(Exception):
    """Expected application failure that carries a registry-backed error code."""

    _default_headers: ClassVar[Mapping[str, str] | None] = None
    headers: Mapping[str, str] | None

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        description: str | None = None,
        details: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        log_level: int | None = None,
    ) -> None:
        """
        Initialize an application error with registry defaults and overrides.

        Purpose:
            Carry the error code and optional per-instance overrides so the API
            layer can render a consistent error response without importing
            transport-specific types.

        Args:
            code: Stable machine-readable error code.
            message: Technical English message. Defaults to the registry
                message.
            description: Client-facing Russian description. Defaults to the
                registry description.
            details: Optional JSON-serializable details.
            headers: Optional response headers associated with the error.
                Defaults to the class-level headers of the concrete error.
            log_level: Logging level for client errors. Defaults to the
                registry level.

        Notes:
            - `details` and `headers` are copied into plain dicts.
            - The class default headers are used only when `headers` is
              ``None``; an explicit empty mapping is preserved.
            - The resolved message is also passed to ``Exception`` for standard
              exception reporting.
        """
        spec = get_error_spec(code)
        self.code = code
        self.message = spec.message if message is None else message
        self.description = spec.description if description is None else description
        self.log_level = spec.log_level if log_level is None else log_level
        self.details = dict(details) if details is not None else None
        if headers is not None:
            self.headers = dict(headers)
        elif self._default_headers is not None:
            self.headers = dict(self._default_headers)
        else:
            self.headers = None
        super().__init__(self.message)


class SemanticAppError(AppError):
    """Application error that derives its code from a semantic subclass."""

    _code: ClassVar[ErrorCode]

    def __init__(
        self,
        message: str | None = None,
        *,
        description: str | None = None,
        details: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        log_level: int | None = None,
    ) -> None:
        """
        Initialize a semantic application error using its class error code.

        Purpose:
            Let domain subclasses declare a fixed error code while callers pass
            only the failure-specific overrides.

        Args:
            message: Technical English message. Defaults to the registry
                message.
            description: Client-facing Russian description. Defaults to the
                registry description.
            details: Optional JSON-serializable details.
            headers: Optional response headers associated with the error.
            log_level: Logging level for client errors. Defaults to the
                registry level.
        """
        super().__init__(
            self._code,
            message,
            description=description,
            details=details,
            headers=headers,
            log_level=log_level,
        )


class BadRequestError(SemanticAppError):
    """Raised when a request is malformed or invalid."""

    _code = ErrorCode.BAD_REQUEST


class UnauthorizedError(SemanticAppError):
    """Raised when authentication is missing or invalid."""

    _code = ErrorCode.UNAUTHORIZED
    _default_headers = {"WWW-Authenticate": "Bearer"}


class ForbiddenError(SemanticAppError):
    """Raised when the caller lacks permission for an operation."""

    _code = ErrorCode.FORBIDDEN


class NotFoundError(SemanticAppError):
    """Raised when a requested resource does not exist."""

    _code = ErrorCode.NOT_FOUND


class ConflictError(SemanticAppError):
    """Raised when an operation conflicts with current state."""

    _code = ErrorCode.CONFLICT


class RateLimitedError(SemanticAppError):
    """Raised when a caller exceeds an allowed request rate."""

    _code = ErrorCode.RATE_LIMITED


class ServiceUnavailableError(SemanticAppError):
    """Raised when a required service is temporarily unavailable."""

    _code = ErrorCode.SERVICE_UNAVAILABLE
