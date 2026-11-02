from __future__ import annotations

from fastapi.exceptions import RequestValidationError

from backend.api.errors.schemas import FieldError, ValidationCode

_VALIDATION_DESCRIPTIONS: dict[ValidationCode, str] = {
    ValidationCode.REQUIRED: "Поле обязательно",
    ValidationCode.INVALID_TYPE: "Некорректный тип значения",
    ValidationCode.INVALID_FORMAT: "Некорректный формат",
    ValidationCode.TOO_SHORT: "Значение слишком короткое",
    ValidationCode.TOO_LONG: "Значение слишком длинное",
    ValidationCode.OUT_OF_RANGE: "Значение вне допустимого диапазона",
    ValidationCode.INVALID_VALUE: "Некорректное значение",
    ValidationCode.INVALID_JSON: "Некорректный JSON",
}

_EXACT_VALIDATION_CODES: dict[str, ValidationCode] = {
    "missing": ValidationCode.REQUIRED,
    "json_invalid": ValidationCode.INVALID_JSON,
    "greater_than": ValidationCode.OUT_OF_RANGE,
    "greater_than_equal": ValidationCode.OUT_OF_RANGE,
    "less_than": ValidationCode.OUT_OF_RANGE,
    "less_than_equal": ValidationCode.OUT_OF_RANGE,
}


def _classify_validation_code(error_type: str) -> ValidationCode:
    """
    Classify a Pydantic error type into a stable validation code.

    Purpose:
        Keep the public validation contract independent of Pydantic's internal
        error type names while preserving common failure categories.

    Args:
        error_type: Pydantic error type name.

    Returns:
        ValidationCode: The normalized validation code for the error type.

    Notes:
        - Format checks run before the generic parsing check so types such as
          ``url_parsing`` are reported as invalid format.
    """
    exact = _EXACT_VALIDATION_CODES.get(error_type)
    if exact is not None:
        return exact
    if "too_short" in error_type:
        return ValidationCode.TOO_SHORT
    if "too_long" in error_type:
        return ValidationCode.TOO_LONG
    if any(marker in error_type for marker in ("pattern", "url", "email")):
        return ValidationCode.INVALID_FORMAT
    if "parsing" in error_type or "_type" in error_type:
        return ValidationCode.INVALID_TYPE
    return ValidationCode.INVALID_VALUE


def normalize_validation_errors(exc: RequestValidationError) -> list[FieldError]:
    """
    Normalize Pydantic validation failures into public field errors.

    Purpose:
        Convert transport-level request validation errors into stable field
        paths, codes, and client-facing descriptions.

    Args:
        exc: Request validation error raised while parsing the request.

    Returns:
        list[FieldError]: One normalized entry per Pydantic error.

    Notes:
        - The field path uses a dotted representation of the error location and
          falls back to ``"request"`` for an empty location.
        - Rejected input values and Pydantic messages are never copied into the
          result.
    """
    fields: list[FieldError] = []
    for error in exc.errors():
        code = _classify_validation_code(str(error.get("type", "")))
        location = error.get("loc", ())
        field = ".".join(str(part) for part in location) if location else "request"
        fields.append(
            FieldError(
                field=field,
                code=code,
                description=_VALIDATION_DESCRIPTIONS[code],
            )
        )
    return fields
