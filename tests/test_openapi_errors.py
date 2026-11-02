from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from backend.api.errors.openapi import StandardizedAPIRouter


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
