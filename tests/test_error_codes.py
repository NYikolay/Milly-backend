from __future__ import annotations

from backend.core.errors import ERROR_SPECS, ErrorCode, get_error_spec


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
    """Use HTTP error statuses for every registered specification."""
    for spec in ERROR_SPECS.values():
        assert 400 <= spec.status_code <= 599


def test_get_error_spec_returns_registered_spec() -> None:
    """Return the registered specification for a known error code."""
    spec = get_error_spec(ErrorCode.BAD_REQUEST)
    assert spec.status_code == 400
    assert spec.description == "Некорректный запрос"
