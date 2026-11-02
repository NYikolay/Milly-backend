from __future__ import annotations

from fastapi.exceptions import RequestValidationError

from backend.api.errors.schemas import FieldError, ValidationCode
from backend.api.errors.validation import normalize_validation_errors


def test_missing_field_is_required() -> None:
    """Classify a missing field as required."""
    exc = RequestValidationError(
        [{"type": "missing", "loc": ("body", "email"), "msg": "Field required", "input": {}}]
    )
    assert normalize_validation_errors(exc) == [
        FieldError(field="body.email", code=ValidationCode.REQUIRED, description="Поле обязательно")
    ]


def test_unknown_pydantic_type_falls_back_to_invalid_value() -> None:
    """Fall back to an invalid value for unknown Pydantic error types."""
    exc = RequestValidationError(
        [{"type": "some_future_error", "loc": ("body", "value"), "msg": "bad", "input": "x"}]
    )
    assert normalize_validation_errors(exc)[0].code == ValidationCode.INVALID_VALUE


def test_nested_location_uses_dotted_path() -> None:
    """Render nested validation locations as dotted field paths."""
    exc = RequestValidationError(
        [{"type": "string_type", "loc": ("body", "items", 0, "name"), "msg": "bad", "input": 1}]
    )
    assert normalize_validation_errors(exc)[0].field == "body.items.0.name"


def test_json_invalid_is_normalized() -> None:
    """Classify invalid JSON bodies separately from field validation."""
    exc = RequestValidationError(
        [{"type": "json_invalid", "loc": ("body",), "msg": "JSON decode error", "input": {}}]
    )
    assert normalize_validation_errors(exc)[0].code == ValidationCode.INVALID_JSON


def test_url_parsing_is_invalid_format() -> None:
    """Classify URL parsing failures as invalid format."""
    exc = RequestValidationError(
        [{"type": "url_parsing", "loc": ("body", "url"), "msg": "bad url", "input": "x"}]
    )
    assert normalize_validation_errors(exc)[0].code == ValidationCode.INVALID_FORMAT
