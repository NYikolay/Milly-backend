from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.core.errors import ErrorCode


class ValidationCode(StrEnum):
    """Stable machine-readable codes for request validation failures."""

    REQUIRED = "required"
    INVALID_TYPE = "invalid_type"
    INVALID_FORMAT = "invalid_format"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    OUT_OF_RANGE = "out_of_range"
    INVALID_VALUE = "invalid_value"
    INVALID_JSON = "invalid_json"


class FieldError(BaseModel):
    """One normalized request validation failure."""

    field: str
    code: ValidationCode
    description: str


class ErrorDetails(BaseModel):
    """Structured error details with optional validation field failures."""

    model_config = ConfigDict(extra="allow")

    fields: list[FieldError] | None = None


class ErrorResponse(BaseModel):
    """Flat API error envelope shared by every failure response."""

    error: Literal[True] = True
    code: ErrorCode
    message: str
    description: str
    details: ErrorDetails | None = None
    request_id: str
