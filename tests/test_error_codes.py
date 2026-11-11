from __future__ import annotations

from backend.api.errors.status import ERROR_STATUS_CODES, status_code_for
from backend.application.auth.errors import AuthNotConfiguredError
from backend.core.errors import (
    ERROR_SPECS,
    ErrorCode,
    PayloadTooLargeError,
    get_error_spec,
)


def test_error_status_map_covers_every_code() -> None:
    """Transport statuses are registered for every machine-readable code."""
    assert set(ERROR_STATUS_CODES) == set(ErrorCode)
    assert all(400 <= status <= 599 for status in ERROR_STATUS_CODES.values())


def test_error_spec_no_longer_owns_http_status() -> None:
    """The core error registry is transport-independent."""
    spec = get_error_spec(ErrorCode.BAD_REQUEST)
    assert not hasattr(spec, "status_code")
    assert status_code_for(ErrorCode.BAD_REQUEST) == 400


def test_every_error_code_has_exactly_one_spec() -> None:
    """Register exactly one specification for every error code."""
    assert set(ERROR_SPECS) == set(ErrorCode)


def test_error_code_values_are_unique_and_stable() -> None:
    """Keep error code wire values unique and stable."""
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))
    assert ErrorCode.NOT_FOUND.value == "not_found"
    assert ErrorCode.INTERNAL_ERROR.value == "internal_error"


def test_status_codes_are_http_errors() -> None:
    """Use HTTP error statuses for every registered error code."""
    for status in ERROR_STATUS_CODES.values():
        assert 400 <= status <= 599


def test_get_error_spec_returns_registered_spec() -> None:
    """Return the registered specification for a known error code."""
    spec = get_error_spec(ErrorCode.BAD_REQUEST)
    assert status_code_for(ErrorCode.BAD_REQUEST) == 400
    assert spec.description == "Некорректный запрос"


def test_auth_not_configured_spec() -> None:
    """Register the exact authentication-not-configured contract."""
    spec = get_error_spec(ErrorCode.AUTH_NOT_CONFIGURED)
    assert status_code_for(ErrorCode.AUTH_NOT_CONFIGURED) == 503
    assert spec.message == "Authentication is not configured"
    assert spec.description == "Аутентификация не настроена"


def test_payload_too_large_spec() -> None:
    """Register the exact payload-too-large contract."""
    spec = get_error_spec(ErrorCode.PAYLOAD_TOO_LARGE)
    assert status_code_for(ErrorCode.PAYLOAD_TOO_LARGE) == 413
    assert spec.message == "Payload too large"
    assert spec.description == "Слишком большой запрос"


def test_payload_too_large_error_uses_registered_spec() -> None:
    """Build the semantic error from the registered specification."""
    exc = PayloadTooLargeError()
    spec = get_error_spec(exc.code)
    assert exc.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert status_code_for(exc.code) == 413
    assert exc.message == spec.message
    assert exc.description == spec.description


def test_auth_not_configured_error_uses_registered_spec() -> None:
    """Build the semantic error from the registered specification."""
    exc = AuthNotConfiguredError()
    spec = get_error_spec(exc.code)
    assert exc.code is ErrorCode.AUTH_NOT_CONFIGURED
    assert status_code_for(exc.code) == 503
    assert exc.message == spec.message
    assert exc.description == spec.description
