from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from starlette.types import Scope

from backend.api.errors.handlers import render_error_response
from backend.api.errors.mapping import payload_from_app_error
from backend.application.auth.errors import AuthNotConfiguredError
from backend.core.config import Settings
from backend.core.errors import AppError, ErrorCode, NotFoundError, UnauthorizedError
from backend.main import create_app


class Payload(BaseModel):
    """Request payload used to trigger validation errors."""

    value: int


def _assert_error_envelope(body: dict[str, object]) -> None:
    """Assert the flat error envelope shape shared by every failure."""
    assert body["error"] is True
    assert isinstance(body["code"], str)
    assert isinstance(body["message"], str)
    assert isinstance(body["description"], str)
    assert isinstance(body["request_id"], str)
    assert "error_id" not in body


async def test_unknown_route_uses_flat_envelope(client: AsyncClient) -> None:
    """Return the flat error envelope for an unknown route."""
    response = await client.get("/missing")
    assert response.status_code == 404
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "not_found"
    assert body["description"] == "Запрошенный ресурс не найден"


async def test_app_error_uses_registry_status_and_description(
    app: FastAPI, client: AsyncClient
) -> None:
    """Render an application error from its registry specification."""

    async def not_found() -> None:
        """Raise a registry-backed not-found error."""
        raise NotFoundError()

    app.add_api_route("/_test/app-error", not_found, methods=["GET"])
    response = await client.get("/_test/app-error")
    assert response.status_code == 404
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "not_found"


async def test_validation_error_envelope(app: FastAPI, client: AsyncClient) -> None:
    """Return validation field details without echoing the input."""

    async def handler(payload: Payload) -> dict[str, int]:
        """Echo the validated payload value."""
        return {"value": payload.value}

    app.add_api_route("/_test/validate", handler, methods=["POST"])
    response = await client.post("/_test/validate", json={"value": "not-an-int"})
    assert response.status_code == 422
    body = response.json()
    _assert_error_envelope(body)
    assert body["details"] == {
        "fields": [
            {
                "field": "body.value",
                "code": "invalid_type",
                "description": "Некорректный тип значения",
            }
        ]
    }
    assert "not-an-int" not in response.text


async def test_malformed_json_returns_400(app: FastAPI, client: AsyncClient) -> None:
    """Map malformed JSON bodies to a bad-request envelope."""

    async def handler(payload: Payload) -> dict[str, int]:
        """Echo the validated payload value."""
        return {"value": payload.value}

    app.add_api_route("/_test/validate", handler, methods=["POST"])
    response = await client.post(
        "/_test/validate",
        content="{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "bad_request"
    assert body["details"]["fields"][0]["code"] == "invalid_json"


async def test_http_exception_4xx_preserves_headers(app: FastAPI, client: AsyncClient) -> None:
    """Preserve HTTP exception headers and detail message for client errors."""

    async def unauthorized() -> None:
        """Raise an unauthorized HTTP error with an authentication challenge."""
        raise HTTPException(
            status_code=401,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    app.add_api_route("/_test/http-error", unauthorized, methods=["GET"])
    response = await client.get("/_test/http-error")
    assert response.status_code == 401
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "unauthorized"
    assert body["message"] == "Token expired"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_http_exception_5xx_hides_detail(app: FastAPI, client: AsyncClient) -> None:
    """Hide HTTP exception details for server errors."""

    async def failing() -> None:
        """Raise a server-side HTTP error with a secret detail."""
        raise HTTPException(status_code=500, detail="secret")

    app.add_api_route("/_test/http-error", failing, methods=["GET"])
    response = await client.get("/_test/http-error")
    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "internal_error"
    assert body["message"] == "Internal server error"
    assert "secret" not in response.text


async def test_http_exception_unknown_status_preserves_status(
    app: FastAPI, client: AsyncClient
) -> None:
    """Preserve an unmapped HTTP status and use the generic error code."""

    async def teapot() -> None:
        """Raise an unmapped HTTP status."""
        raise HTTPException(status_code=418, detail="teapot")

    app.add_api_route("/_test/http-error", teapot, methods=["GET"])
    response = await client.get("/_test/http-error")
    assert response.status_code == 418
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "http_error"


async def test_http_exception_non_string_detail_is_structured(
    app: FastAPI, client: AsyncClient
) -> None:
    """Expose a non-string HTTP exception detail as structured details."""

    async def conflict() -> None:
        """Raise a conflict with a structured detail."""
        raise HTTPException(status_code=409, detail={"reason": "duplicate"})

    app.add_api_route("/_test/http-error", conflict, methods=["GET"])
    response = await client.get("/_test/http-error")
    assert response.status_code == 409
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "conflict"
    assert body["details"] == {"detail": {"reason": "duplicate"}}


async def test_500_hides_details_in_production() -> None:
    """Hide unexpected error details when debug mode is off."""

    app = create_app(
        Settings(
            _env_file=None,
            app_env="production",
            debug=False,
            log_json=False,
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
        )
    )

    async def boom() -> None:
        """Raise an unexpected error."""
        raise RuntimeError("secret details")

    app.add_api_route("/_test/boom", boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/boom")
    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "internal_error"
    assert "details" not in body
    assert "secret details" not in response.text


async def test_500_in_debug_includes_details() -> None:
    """Expose unexpected error details when debug mode is on."""

    app = create_app(Settings(_env_file=None, app_env="local", debug=True, log_json=False))

    async def boom() -> None:
        """Raise an unexpected error."""
        raise RuntimeError("secret details")

    app.add_api_route("/_test/boom", boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/boom")
    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "internal_error"
    assert body["details"] == {"exception": "RuntimeError", "message": "secret details"}


async def test_500_includes_security_and_cors_headers() -> None:
    """Keep security and CORS headers on responses from the outer error middleware."""

    app = create_app(
        Settings(
            _env_file=None,
            app_env="production",
            debug=False,
            log_json=False,
            cors_origins=["http://example.com"],
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
        )
    )

    async def boom() -> None:
        """Raise an unexpected error."""
        raise RuntimeError("boom")

    app.add_api_route("/_test/boom", boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get(
            "/_test/boom",
            headers={"Origin": "http://example.com", "X-Request-ID": "test-request-id"},
        )
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["access-control-allow-origin"] == "http://example.com"
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["access-control-expose-headers"] == "X-Request-ID"
    assert response.headers["Vary"] == "Origin"
    assert response.headers["X-Request-ID"] == "test-request-id"


async def test_request_id_matches_header(client: AsyncClient) -> None:
    """Echo the incoming request ID in both the envelope and headers."""
    response = await client.get("/missing", headers={"X-Request-ID": "test-request-id"})
    assert response.status_code == 404
    body = response.json()
    _assert_error_envelope(body)
    assert body["request_id"] == "test-request-id"
    assert response.headers["X-Request-ID"] == "test-request-id"


async def test_response_validation_error_returns_500(app: FastAPI) -> None:
    """Map response validation failures to an internal error envelope."""

    async def bad_response() -> str:
        """Return a value that violates the declared response model."""
        return "not-an-int"

    app.add_api_route("/_test/response", bad_response, methods=["GET"], response_model=int)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/response")
    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "internal_error"


async def test_unserializable_details_are_dropped(app: FastAPI, client: AsyncClient) -> None:
    """Drop details that cannot be serialized to JSON."""

    async def conflict() -> None:
        """Raise an application error with an unserializable detail."""
        raise AppError(ErrorCode.CONFLICT, "custom", details={"obj": object()})

    app.add_api_route("/_test/unserializable", conflict, methods=["GET"])
    response = await client.get("/_test/unserializable")
    assert response.status_code == 409
    body = response.json()
    _assert_error_envelope(body)
    assert "details" not in body


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
    ],
)
async def test_non_finite_details_keep_intended_status(
    app: FastAPI,
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
    value: float,
) -> None:
    """Drop non-finite details instead of turning a client error into a 500."""

    async def conflict() -> None:
        """Raise a conflict whose details cannot be serialized to strict JSON."""
        raise AppError(ErrorCode.CONFLICT, "custom", details={"value": value})

    app.add_api_route("/_test/non-finite", conflict, methods=["GET"])
    with caplog.at_level(logging.WARNING):
        response = await client.get("/_test/non-finite")
    assert response.status_code == 409
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "conflict"
    assert "details" not in body
    assert "api.error_details_dropped" in caplog.text


async def test_explicit_internal_error_hides_details_in_production() -> None:
    """Drop details of an explicit internal error outside debug mode."""

    app = create_app(
        Settings(
            _env_file=None,
            app_env="production",
            debug=False,
            log_json=False,
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
        )
    )

    async def internal() -> None:
        """Raise an internal application error with details."""
        raise AppError(ErrorCode.INTERNAL_ERROR, details={"internal": "detail"})

    app.add_api_route("/_test/internal", internal, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/internal")
    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "internal_error"
    assert "details" not in body
    assert "detail" not in response.text


async def test_explicit_internal_error_keeps_details_in_debug() -> None:
    """Keep details of an explicit internal error in debug mode."""

    app = create_app(Settings(_env_file=None, app_env="local", debug=True, log_json=False))

    async def internal() -> None:
        """Raise an internal application error with details."""
        raise AppError(ErrorCode.INTERNAL_ERROR, details={"internal": "detail"})

    app.add_api_route("/_test/internal", internal, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/_test/internal")
    assert response.status_code == 500
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "internal_error"
    assert body["details"] == {"internal": "detail"}


async def test_app_error_headers_are_preserved(app: FastAPI, client: AsyncClient) -> None:
    """Preserve headers declared on an application error."""

    async def conflict() -> None:
        """Raise an application error with a response header."""
        raise AppError(ErrorCode.CONFLICT, "custom", headers={"Retry-After": "1"})

    app.add_api_route("/_test/headers", conflict, methods=["GET"])
    response = await client.get("/_test/headers")
    assert response.status_code == 409
    assert response.headers["retry-after"] == "1"


async def test_unauthorized_error_defaults_to_bearer_challenge(
    app: FastAPI, client: AsyncClient
) -> None:
    """Add a bearer challenge to unauthorized errors by default."""

    async def unauthorized() -> None:
        """Raise an unauthorized application error."""
        raise UnauthorizedError()

    app.add_api_route("/_test/unauthorized", unauthorized, methods=["GET"])
    response = await client.get("/_test/unauthorized")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


async def test_auth_not_configured_error_renders_503(app: FastAPI, client: AsyncClient) -> None:
    """Render the authentication-not-configured error from its registry spec."""

    async def not_configured() -> None:
        """Raise an authentication-not-configured application error."""
        raise AuthNotConfiguredError()

    app.add_api_route("/_test/auth-not-configured", not_configured, methods=["GET"])
    response = await client.get("/_test/auth-not-configured")

    assert response.status_code == 503
    body = response.json()
    _assert_error_envelope(body)
    assert body["code"] == "auth_not_configured"
    assert body["message"] == "Authentication is not configured"
    assert body["description"] == "Аутентификация не настроена"


@pytest.mark.parametrize("header_name", ["X-Request-ID", "x-request-id"])
def test_app_error_cannot_override_request_id_header(header_name: str) -> None:
    """Discard conflicting request ID headers supplied by an application error."""
    exc = AppError(ErrorCode.CONFLICT, "custom", headers={header_name: "other"})
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/_test/request-id",
        "headers": [],
        "state": {"request_id": "test-request-id"},
    }
    response = render_error_response(
        payload_from_app_error(exc),
        scope=scope,
        settings=Settings(_env_file=None, log_json=False),
    )
    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    header_values = response.headers.getlist("x-request-id")
    assert header_values == ["test-request-id"]
    assert body["request_id"] == header_values[0]
    assert "other" not in header_values


def test_app_error_payload_uses_overrides() -> None:
    """Use per-instance overrides instead of registry defaults."""
    exc = AppError(
        ErrorCode.CONFLICT,
        "custom",
        description="custom description",
        details={"key": "value"},
        headers={"Retry-After": "1"},
        log_level=logging.ERROR,
    )
    payload = payload_from_app_error(exc)
    assert payload.status_code == 409
    assert payload.message == "custom"
    assert payload.description == "custom description"
    assert payload.log_level == logging.ERROR
    assert payload.details == {"key": "value"}
    assert payload.headers == {"Retry-After": "1"}


async def test_docs_disabled_in_production() -> None:
    """Disable interactive docs when the environment is production."""
    app = create_app(
        Settings(
            _env_file=None,
            app_env="production",
            log_json=False,
            telegram_bot_token="123:token",
            auth_jwt_secret="a" * 32,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.get("/docs")
    assert response.status_code == 404


async def test_error_log_contains_code_and_request_id(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """Log the error code and request ID without exposing details."""

    async def boom() -> None:
        """Raise an unexpected error."""
        raise RuntimeError("boom")

    app.add_api_route("/_test/log", boom, methods=["GET"])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.ERROR):
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            await http_client.get("/_test/log", headers={"X-Request-ID": "log-id"})
    assert "api.error" in caplog.text
    assert "internal_error" in caplog.text
    assert "log-id" in caplog.text


async def test_validation_log_does_not_include_input(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Never write rejected request input to logs."""

    async def handler(payload: Payload) -> dict[str, int]:
        """Echo the validated payload value."""
        return {"value": payload.value}

    app.add_api_route("/_test/validate-log", handler, methods=["POST"])
    with caplog.at_level(logging.INFO):
        await client.post("/_test/validate-log", json={"value": "not-an-int"})
    assert "not-an-int" not in caplog.text
