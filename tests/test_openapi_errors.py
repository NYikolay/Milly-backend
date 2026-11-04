from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from backend.api.errors.openapi import StandardizedAPIRouter, document_errors
from backend.api.errors.schemas import ErrorResponse
from backend.core.errors import ErrorCode


class Body(BaseModel):
    """Request body used to exercise validation error documentation."""

    value: int


def test_error_response_component(app: FastAPI) -> None:
    """Expose the unified error envelope and its nested schemas as components."""
    components = app.openapi()["components"]["schemas"]
    assert "ErrorResponse" in components
    assert {"error", "code", "message", "description", "request_id"} <= set(
        components["ErrorResponse"]["properties"]
    )
    assert "ErrorCode" in components
    assert "FieldError" in components
    assert "ValidationCode" in components


def test_422_uses_error_response(app: FastAPI) -> None:
    """Document request validation failures with the unified error envelope."""
    router = StandardizedAPIRouter()

    @router.post("/_test/body")
    async def body(payload: Body) -> dict[str, int]:
        return {"value": payload.value}

    app.include_router(router)
    operation = app.openapi()["paths"]["/_test/body"]["post"]
    ref = operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ErrorResponse")
    assert "HTTPValidationError" not in str(operation)


def test_common_error_responses(app: FastAPI) -> None:
    """Document every common failure status on a standardized operation."""
    operation = app.openapi()["paths"]["/health/live"]["get"]
    for status in ("400", "404", "422", "500", "503"):
        ref = operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")


def test_document_errors_maps_registry_statuses() -> None:
    """Derive response statuses from the error registry and merge shared ones."""
    responses = document_errors(
        {
            ErrorCode.FORBIDDEN: "The request origin is not allowed.",
            ErrorCode.AUTH_NOT_CONFIGURED: "Authentication settings are missing.",
            ErrorCode.DATABASE_UNAVAILABLE: "The database probe failed.",
        }
    )

    assert set(responses) == {403, 503}
    assert responses[403]["model"] is ErrorResponse
    assert responses[403]["description"] == ("- forbidden: The request origin is not allowed.")
    assert responses[503]["description"] == (
        "- auth_not_configured: Authentication settings are missing.\n"
        "- database_unavailable: The database probe failed."
    )


@pytest.mark.parametrize(
    ("path", "method", "expected_codes"),
    [
        (
            "/api/v1/auth/telegram",
            "post",
            {
                "401": "- unauthorized:",
                "403": "- forbidden:",
                "503": "- auth_not_configured:",
            },
        ),
        (
            "/api/v1/auth/refresh",
            "post",
            {
                "401": "- unauthorized:",
                "403": "- forbidden:",
                "503": "- auth_not_configured:",
            },
        ),
        ("/api/v1/auth/logout", "post", {"403": "- forbidden:"}),
        (
            "/api/v1/auth/me",
            "get",
            {"401": "- unauthorized:", "503": "- auth_not_configured:"},
        ),
        (
            "/health/ready",
            "get",
            {"503": "- database_unavailable:"},
        ),
    ],
)
def test_operations_document_specific_error_codes(
    app: FastAPI,
    path: str,
    method: str,
    expected_codes: dict[str, str],
) -> None:
    """Document concrete statuses and codes for every operation-specific error."""
    operation = app.openapi()["paths"][path][method]

    for status, prefix in expected_codes.items():
        response = operation["responses"][status]
        assert response["description"].startswith(prefix)
        ref = response["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")
