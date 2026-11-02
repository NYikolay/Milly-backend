# API Error Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce one flat error envelope and one central error-code registry for every API failure path in the Milly backend.

**Architecture:** `backend.core.errors` owns the transport-agnostic `ErrorCode`/`ErrorSpec` registry and `AppError` hierarchy. `backend.api.errors` owns the Pydantic envelope, Pydantic validation normalization, exception-to-payload mapping, the single renderer, middleware, and OpenAPI integration. All error paths render through that one API layer.

**Tech Stack:** Python 3.14, FastAPI 0.156.x, Starlette 1.9.x, Pydantic 2.14.x, SQLAlchemy 2.1.x, Taskiq 0.13.x, pytest, structlog.

**Spec:** `docs/superpowers/specs/2026-11-02-api-error-standardization-design.md`

## Global Constraints

- Python `>=3.11,<3.15`; the project targets 3.14 and already uses `StrEnum`.
- Do not add runtime or dev dependencies.
- Follow SOLID + KISS + TDD. Every new or changed function, method, and class gets an English docstring kept accurate with the code.
- No code comments unless a non-obvious algorithm requires one; prefer docstrings.
- `message` is English technical text for logs/developers. `description` is Russian user-facing text.
- `request_id` must always be present and equal to the `X-Request-ID` response header.
- Never put request bodies, passwords, tokens, SQL, stack traces, or `input`/`ctx`/`url` from Pydantic into an error response.
- Do not change success-response shapes.
- Use `poetry run pytest`, `poetry run ruff check .`, `poetry run ruff format --check .`, and `poetry run mypy src tests` as the verification commands.
- Commit after each task only when its tests pass.

## Review Focus

These are the inputs/failure modes most likely to bite and are not fully covered by the obvious happy-path tests:

1. **Non-JSON-serializable domain `details`** — must not turn a 4xx into an unhandled 500; the renderer drops details and still returns the intended error. Covered by `test_unserializable_details_are_dropped` in Task 1.
2. **`HTTPException(500, detail="secret")`** — must never leak the detail to the client. Covered by `test_http_exception_5xx_hides_detail` in Task 1.
3. **Error raised before `RequestContextMiddleware` can set state** — must still produce an envelope with a generated `request_id`. Covered by `test_middleware_error_uses_envelope` in Task 3.
4. **Malformed or empty JSON body** — must be `400 bad_request` with `invalid_json`, not `422 validation_error`. Covered by `test_malformed_json_returns_400` in Task 1.
5. **SQLAlchemy failure during dependency teardown** — must still render the unified envelope; `tests/test_deps.py` is updated in Task 1 and `test_operational_error_maps_to_database_unavailable` in Task 2 pins the 503 mapping.

## File Structure

**Create:**
- `src/backend/api/errors/__init__.py` — public exports for the error API layer.
- `src/backend/api/errors/schemas.py` — `ValidationCode`, `FieldError`, `ErrorDetails`, `ErrorResponse`.
- `src/backend/api/errors/validation.py` — Pydantic error normalization.
- `src/backend/api/errors/mapping.py` — `ErrorPayload` and exception-to-payload mappers.
- `src/backend/api/errors/handlers.py` — the one renderer and FastAPI exception handlers.
- `src/backend/api/errors/middleware.py` — `ErrorEnvelopeMiddleware`, `AllowedHostsMiddleware`.
- `src/backend/api/errors/openapi.py` — `StandardizedAPIRoute`, `StandardizedAPIRouter`, `ERROR_RESPONSES`.
- `tests/test_error_codes.py`
- `tests/test_error_validation.py`
- `tests/test_error_mapping.py`
- `tests/test_error_middleware.py`
- `tests/test_openapi_errors.py`

**Modify:**
- `src/backend/core/errors.py` — replace the old status-carrying `AppError` and old handlers with the registry + transport-agnostic exceptions.
- `src/backend/api/schemas.py` — remove `ErrorDetail` and `ErrorResponse`; keep success schemas.
- `src/backend/main.py` — import handlers from `backend.api.errors.handlers`, replace `TrustedHostMiddleware`, wire middleware order.
- `src/backend/api/router.py`, `src/backend/api/routes/health.py`, `src/backend/api/routes/tasks.py` — use `StandardizedAPIRouter`; health raises `AppError` on 503.
- `tests/test_errors.py` — rewrite for the flat envelope.
- `tests/test_deps.py` — update the 500 assertion to the flat envelope.
- `tests/test_tasks_api.py` — Task 1 updates the 500 assertion; Task 2 changes the broker failure to 503.
- `tests/test_health.py` — readiness 503 envelope and disallowed-host envelope.
- `README.md` — document the contract.

**Delete:** none (the old `core/errors.py` is replaced in place).

---

### Task 1: Unified error contract foundation

**Files:**
- Replace: `src/backend/core/errors.py`
- Create: `src/backend/api/errors/__init__.py`
- Create: `src/backend/api/errors/schemas.py`
- Create: `src/backend/api/errors/validation.py`
- Create: `src/backend/api/errors/mapping.py`
- Create: `src/backend/api/errors/handlers.py`
- Modify: `src/backend/api/schemas.py`
- Modify: `src/backend/main.py:18`
- Modify: `tests/test_deps.py:217-218`
- Modify: `tests/test_tasks_api.py:42-43`
- Rewrite: `tests/test_errors.py`
- Create: `tests/test_error_codes.py`
- Create: `tests/test_error_validation.py`

**Interfaces:**
- Consumes: `SECURITY_HEADERS` from `backend.core.middleware`; `Settings` from `backend.core.config`.
- Produces:
  - `ErrorCode` (`StrEnum`), `ErrorSpec` (frozen dataclass with `status_code: int`, `log_level: int`, `message: str`, `description: str`), `ERROR_SPECS: dict[ErrorCode, ErrorSpec]`, `get_error_spec(code: ErrorCode) -> ErrorSpec`.
  - `AppError(code: ErrorCode, message: str | None = None, *, description: str | None = None, details: Mapping[str, object] | None = None, headers: Mapping[str, str] | None = None, log_level: int | None = None)` with attributes of the same names.
  - `SemanticAppError` with `_code: ClassVar[ErrorCode]`; subclasses `BadRequestError`, `UnauthorizedError`, `ForbiddenError`, `NotFoundError`, `ConflictError`, `RateLimitedError`, `ServiceUnavailableError`.
  - `ValidationCode` (`StrEnum`), `FieldError`, `ErrorDetails`, `ErrorResponse` in `api.errors.schemas`.
  - `normalize_validation_errors(exc: RequestValidationError) -> list[FieldError]`.
  - `ErrorPayload` frozen dataclass with `status_code: int`, `code: ErrorCode`, `message: str`, `description: str`, `log_level: int`, `details: Mapping[str, object] | None = None`, `headers: Mapping[str, str] | None = None`.
  - `payload_from_app_error`, `payload_from_http_exception`, `payload_from_validation_error`, `payload_from_unexpected_error`.
  - `resolve_request_id(scope: Scope) -> str`, `render_error_response(...) -> JSONResponse`, `register_exception_handlers(app: FastAPI) -> None`.
- Later tasks import these exact names. Do not rename them.

- [ ] **Step 1: Write the failing registry tests**

Create `tests/test_error_codes.py`:

```python
from __future__ import annotations

from backend.core.errors import ERROR_SPECS, ErrorCode, get_error_spec


def test_every_error_code_has_exactly_one_spec() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)


def test_error_code_values_are_unique_and_stable() -> None:
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))
    assert ErrorCode.NOT_FOUND.value == "not_found"
    assert ErrorCode.INTERNAL_ERROR.value == "internal_error"


def test_status_codes_are_http_errors() -> None:
    for spec in ERROR_SPECS.values():
        assert 400 <= spec.status_code <= 599


def test_get_error_spec_returns_registered_spec() -> None:
    spec = get_error_spec(ErrorCode.BAD_REQUEST)
    assert spec.status_code == 400
    assert spec.description == "Некорректный запрос"
```

- [ ] **Step 2: Run the registry tests to verify they fail**

Run: `poetry run pytest tests/test_error_codes.py -v`
Expected: FAIL with `ImportError` because `ErrorCode` and `ERROR_SPECS` do not exist yet.

- [ ] **Step 3: Write the failing contract and validation tests**

Create `tests/test_error_validation.py`:

```python
from __future__ import annotations

from fastapi.exceptions import RequestValidationError

from backend.api.errors.schemas import FieldError, ValidationCode
from backend.api.errors.validation import normalize_validation_errors


def test_missing_field_is_required() -> None:
    exc = RequestValidationError(
        [{"type": "missing", "loc": ("body", "email"), "msg": "Field required", "input": {}}]
    )
    assert normalize_validation_errors(exc) == [
        FieldError(field="body.email", code=ValidationCode.REQUIRED, description="Поле обязательно")
    ]


def test_unknown_pydantic_type_falls_back_to_invalid_value() -> None:
    exc = RequestValidationError(
        [{"type": "some_future_error", "loc": ("body", "value"), "msg": "bad", "input": "x"}]
    )
    assert normalize_validation_errors(exc)[0].code == ValidationCode.INVALID_VALUE


def test_nested_location_uses_dotted_path() -> None:
    exc = RequestValidationError(
        [{"type": "string_type", "loc": ("body", "items", 0, "name"), "msg": "bad", "input": 1}]
    )
    assert normalize_validation_errors(exc)[0].field == "body.items.0.name"


def test_json_invalid_is_normalized() -> None:
    exc = RequestValidationError(
        [{"type": "json_invalid", "loc": ("body",), "msg": "JSON decode error", "input": {}}]
    )
    assert normalize_validation_errors(exc)[0].code == ValidationCode.INVALID_JSON


def test_url_parsing_is_invalid_format() -> None:
    exc = RequestValidationError(
        [{"type": "url_parsing", "loc": ("body", "url"), "msg": "bad url", "input": "x"}]
    )
    assert normalize_validation_errors(exc)[0].code == ValidationCode.INVALID_FORMAT
```

Rewrite `tests/test_errors.py`. It must include a helper and these tests:

```python
def _assert_error_envelope(body: dict[str, object]) -> None:
    assert body["error"] is True
    assert isinstance(body["code"], str)
    assert isinstance(body["message"], str)
    assert isinstance(body["description"], str)
    assert isinstance(body["request_id"], str)
    assert "error_id" not in body
```

Test names and exact assertions:

- `test_unknown_route_uses_flat_envelope` — `GET /missing` is 404; `code == "not_found"`; `description == "Запрошенный ресурс не найден"`; envelope helper passes.
- `test_app_error_uses_registry_status_and_description` — a route raising `NotFoundError()` is 404 with `code == "not_found"`.
- `test_validation_error_envelope` — a route accepting `Payload(value: int)` returns 422 for `{"value": "not-an-int"}`; `details == {"fields": [{"field": "body.value", "code": "invalid_type", "description": "Некорректный тип значения"}]}`; `"not-an-int" not in response.text`.
- `test_malformed_json_returns_400` — `POST` with raw `"{"` and `Content-Type: application/json` returns 400; `code == "bad_request"`; `details["fields"][0]["code"] == "invalid_json"`.
- `test_http_exception_4xx_preserves_headers` — a route raising `HTTPException(401, detail="Token expired", headers={"WWW-Authenticate": "Bearer"})` returns 401; `code == "unauthorized"`; `message == "Token expired"`; header `www-authenticate == "Bearer"`.
- `test_http_exception_5xx_hides_detail` — a route raising `HTTPException(500, detail="secret")` returns 500; `code == "internal_error"`; `message == "Internal server error"`; `"secret" not in response.text`.
- `test_http_exception_unknown_status_preserves_status` — a route raising `HTTPException(418, detail="teapot")` returns 418; `code == "http_error"`.
- `test_http_exception_non_string_detail_is_structured` — a route raising `HTTPException(409, detail={"reason": "duplicate"})` returns 409; `code == "conflict"`; `details == {"detail": {"reason": "duplicate"}}`.
- `test_500_hides_details_in_production` — `create_app(Settings(_env_file=None, app_env="production", debug=False, log_json=False))`; a route raising `RuntimeError("secret details")` returns 500 with `code == "internal_error"`; `"details" not in body`; `"secret details" not in response.text`.
- `test_500_in_debug_includes_details` — `create_app(Settings(_env_file=None, app_env="local", debug=True, log_json=False))`; the same route returns `details == {"exception": "RuntimeError", "message": "secret details"}`.
- `test_500_includes_security_and_cors_headers` — existing test updated to the flat body; still asserts security headers, CORS headers, and `X-Request-ID` echo.
- `test_request_id_matches_header` — with `X-Request-ID: test-request-id`, the 404 body `request_id == "test-request-id"` and the header matches.
- `test_response_validation_error_returns_500` — a route with `response_model=int` returning `"not-an-int"` is 500 with `code == "internal_error"`.
- `test_unserializable_details_are_dropped` — a route raising `AppError(ErrorCode.CONFLICT, "custom", details={"obj": object()})` returns 409 with `"details" not in body`.
- `test_app_error_headers_are_preserved` — `AppError(ErrorCode.CONFLICT, "custom", headers={"Retry-After": "1"})` returns header `retry-after == "1"`.
- `test_app_error_payload_uses_overrides` — unit-test `payload_from_app_error` directly: `AppError(ErrorCode.CONFLICT, "custom", description="custom description", details={"key": "value"}, headers={"Retry-After": "1"}, log_level=logging.ERROR)` yields status 409, message `"custom"`, description `"custom description"`, log level `logging.ERROR`, details `{"key": "value"}`, headers `{"Retry-After": "1"}`.
- `test_docs_disabled_in_production` — keep the existing test unchanged.
- `test_error_log_contains_code_and_request_id` — use `caplog.at_level(logging.ERROR)` around a 500 request with `X-Request-ID: log-id`; assert `"api.error"`, `"internal_error"`, and `"log-id"` appear in `caplog.text`.
- `test_validation_log_does_not_include_input` — use `caplog.at_level(logging.INFO)` around the invalid `{"value": "not-an-int"}` request; assert `"not-an-int" not in caplog.text`.

- [ ] **Step 4: Run the new tests to verify they fail**

Run: `poetry run pytest tests/test_error_codes.py tests/test_error_validation.py tests/test_errors.py -v`
Expected: FAIL with import errors for `ERROR_SPECS`, `ValidationCode`, `normalize_validation_errors`, and the new API error modules.

- [ ] **Step 5: Replace `src/backend/core/errors.py`**

Implement the exact registry values:

```python
class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
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
```

`ERROR_SPECS` entries (`status`, `log_level`, `message`, `description`):

```text
BAD_REQUEST              400 warning "Bad request"              "Некорректный запрос"
VALIDATION_ERROR         422 info    "Request validation failed" "Проверьте правильность заполнения полей"
UNAUTHORIZED             401 warning "Authentication required"   "Требуется авторизация"
FORBIDDEN                403 warning "Permission denied"         "Недостаточно прав"
NOT_FOUND                404 info    "Resource not found"        "Запрошенный ресурс не найден"
METHOD_NOT_ALLOWED       405 warning "Method not allowed"        "Метод не поддерживается"
CONFLICT                 409 warning "Conflict"                   "Операция конфликтует с текущим состоянием"
PAYLOAD_TOO_LARGE        413 warning "Payload too large"         "Слишком большой запрос"
UNSUPPORTED_MEDIA_TYPE   415 warning "Unsupported media type"    "Неподдерживаемый тип содержимого"
RATE_LIMITED             429 warning "Too many requests"         "Слишком много запросов"
INTERNAL_ERROR           500 error   "Internal server error"     "Внутренняя ошибка сервера"
NOT_IMPLEMENTED          501 warning "Not implemented"           "Функция не реализована"
SERVICE_UNAVAILABLE      503 error   "Service unavailable"       "Сервис временно недоступен"
DATABASE_UNAVAILABLE     503 error   "Database is unavailable"   "База данных временно недоступна"
TASK_ENQUEUE_FAILED      503 error   "Failed to enqueue task"    "Не удалось поставить задачу в очередь"
HTTP_ERROR               500 warning "HTTP error"                "Ошибка запроса"
```

Implement `AppError` and the semantic subclasses exactly as described in the Interfaces block. `SemanticAppError` uses `_code` so domain subclasses can override it without clashing with the instance attribute `code`. `AppError` copies `details` and `headers` into plain dicts and uses `get_error_spec` defaults when `message`/`description`/`log_level` are omitted. `HTTP_ERROR` is the only spec whose status is overridden by the HTTP-exception mapper.

- [ ] **Step 6: Implement the API error layer**

Create `api/errors/schemas.py`:

```python
class ValidationCode(StrEnum):
    REQUIRED = "required"
    INVALID_TYPE = "invalid_type"
    INVALID_FORMAT = "invalid_format"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    OUT_OF_RANGE = "out_of_range"
    INVALID_VALUE = "invalid_value"
    INVALID_JSON = "invalid_json"


class FieldError(BaseModel):
    field: str
    code: ValidationCode
    description: str


class ErrorDetails(BaseModel):
    model_config = ConfigDict(extra="allow")
    fields: list[FieldError] | None = None


class ErrorResponse(BaseModel):
    error: Literal[True] = True
    code: ErrorCode
    message: str
    description: str
    details: ErrorDetails | None = None
    request_id: str
```

Create `api/errors/validation.py` with `normalize_validation_errors`. Map Pydantic types by exact known values and a fallback:

```text
missing -> REQUIRED
json_invalid -> INVALID_JSON
*too_short* -> TOO_SHORT
*too_long* -> TOO_LONG
greater_than, greater_than_equal, less_than, less_than_equal -> OUT_OF_RANGE
*pattern*, *url*, *email* -> INVALID_FORMAT
*parsing*, *_type -> INVALID_TYPE
everything else -> INVALID_VALUE
```

Use these descriptions:

```text
required -> "Поле обязательно"
invalid_type -> "Некорректный тип значения"
invalid_format -> "Некорректный формат"
too_short -> "Значение слишком короткое"
too_long -> "Значение слишком длинное"
out_of_range -> "Значение вне допустимого диапазона"
invalid_value -> "Некорректное значение"
invalid_json -> "Некорректный JSON"
```

`field` is `".".join(str(part) for part in loc)`; use `"request"` if `loc` is empty. Never read `input`, `ctx`, `url`, or `msg` into the response.

Create `api/errors/mapping.py` with `ErrorPayload` and the four mappers. Exact behavior:

- `payload_from_app_error`: status from `get_error_spec(exc.code)`; `message`, `description`, `log_level`, `details`, and `headers` from the exception (its constructor already fills omitted fields from the spec).
- `payload_from_http_exception`: status from `exc.status_code`; code from this table:
  `400 bad_request`, `401 unauthorized`, `403 forbidden`, `404 not_found`, `405 method_not_allowed`, `409 conflict`, `413 payload_too_large`, `415 unsupported_media_type`, `422 validation_error`, `429 rate_limited`, `500 internal_error`, `501 not_implemented`, `503 service_unavailable`; any other status uses `http_error`.
  For 4xx with a string `detail`, use it as `message`; for 4xx with a non-string non-None `detail`, put it in `details={"detail": exc.detail}`; for 5xx ignore `detail` and use the spec message/description. Preserve `exc.headers` for all statuses.
- `payload_from_validation_error`: call `normalize_validation_errors`; status 400 with `bad_request` if any field has `INVALID_JSON`, otherwise 422 with `validation_error`; `details={"fields": [field.model_dump() for field in fields]}`.
- `payload_from_unexpected_error(exc, *, debug)`: status 500, `internal_error`, no details in production; in debug `details={"exception": type(exc).__name__, "message": str(exc)}`.

Create `api/errors/handlers.py` with:

- `resolve_request_id(scope: Scope) -> str` reading `scope["state"]["request_id"]`, then structlog contextvars, then `uuid4().hex`.
- `render_error_response(payload, *, scope, settings, exc=None) -> JSONResponse`:
  - normalize `payload.details` with `jsonable_encoder`; on any serialization or validation failure log `api.error_details_dropped` and use `None`;
  - build `ErrorResponse`, add `SECURITY_HEADERS`, payload headers, `X-Request-ID`, and CORS headers for the request origin when `settings.cors_origins` allows it;
  - serialize with `exclude_none=True`;
  - log `api.error` with `error_code`, `status_code`, `request_id`, `method`, `path`; use `payload.log_level` for 4xx and `logger.error(..., exc_info=exc)` for 5xx; never log `details`.
- handlers `handle_app_error`, `handle_validation_error`, `handle_http_exception`, `handle_response_validation_error`, `handle_unexpected_error`; each asserts its exception type and calls the matching mapper and renderer.
- `register_exception_handlers(app)` registering `AppError`, `RequestValidationError`, `ResponseValidationError`, `StarletteHTTPException`, and `Exception`.

Create `api/errors/__init__.py` exporting the public names used by `main.py` and tests.

- [ ] **Step 7: Wire the new handlers and remove the old schemas**

- In `src/backend/main.py`, change the import to `from backend.api.errors.handlers import register_exception_handlers`.
- In `src/backend/api/schemas.py`, delete `ErrorDetail` and `ErrorResponse`; keep `HealthResponse` and `TaskEnqueuedResponse`.
- Update `tests/test_deps.py:217-218` to assert `response.json()["code"] == "internal_error"`.
- Update `tests/test_tasks_api.py:42-43` to assert `response.json()["code"] == "internal_error"` (Task 2 changes this to 503).

- [ ] **Step 8: Run the focused tests**

Run: `poetry run pytest tests/test_error_codes.py tests/test_error_validation.py tests/test_errors.py tests/test_deps.py tests/test_tasks_api.py -v`
Expected: PASS.

- [ ] **Step 9: Run the full suite**

Run: `poetry run pytest -q`
Expected: PASS. `tests/test_health.py` still passes because health 503 is a normal `HealthResponse` until Task 2, and the host test still only asserts 400.

- [ ] **Step 10: Commit**

```bash
git add src/backend/core/errors.py src/backend/api/errors src/backend/api/schemas.py src/backend/main.py tests/test_error_codes.py tests/test_error_validation.py tests/test_errors.py tests/test_deps.py tests/test_tasks_api.py
git commit -m "feat: add unified api error contract"
```

---

### Task 2: Infrastructure failure mapping and readiness

**Files:**
- Modify: `src/backend/api/errors/mapping.py`
- Modify: `src/backend/api/errors/handlers.py`
- Modify: `src/backend/api/routes/health.py`
- Modify: `tests/test_health.py:38-43`
- Modify: `tests/test_tasks_api.py:30-43`
- Create: `tests/test_error_mapping.py`

**Interfaces:**
- Consumes: `ErrorPayload`, `render_error_response`, `register_exception_handlers` from Task 1.
- Produces:
  - `payload_from_sqlalchemy_error(exc: SQLAlchemyError) -> ErrorPayload`
  - `payload_from_taskiq_error(exc: TaskiqError) -> ErrorPayload`
  - handlers `handle_sqlalchemy_error`, `handle_taskiq_error`, registered in `register_exception_handlers`.
  - `health.ready` raises `AppError(ErrorCode.DATABASE_UNAVAILABLE, message="Database is unavailable", details={"database": "down"})` instead of returning a 503 `HealthResponse`.

- [ ] **Step 1: Write the failing infrastructure-mapping tests**

Create `tests/test_error_mapping.py` with these tests:

- `test_operational_error_maps_to_database_unavailable` — add `GET /_test/operational` raising `OperationalError("SELECT 1", {}, Exception("down"))`; expect 503, `code == "database_unavailable"`, `description == "База данных временно недоступна"`.
- `test_generic_sqlalchemy_error_maps_to_internal_error` — `SQLAlchemyError("boom")`; expect 500, `code == "internal_error"`.
- `test_integrity_error_stays_internal_error` — `IntegrityError("stmt", {}, Exception("duplicate"))`; expect 500, `code == "internal_error"` (business conflicts must be explicit `AppError`s).
- `test_taskiq_error_maps_to_task_enqueue_failed` — `SendTaskError()`; expect 503, `code == "task_enqueue_failed"`, `description == "Не удалось поставить задачу в очередь"`.

Use the existing `app` and `client` fixtures. For 500 assertions use a fresh transport with `raise_app_exceptions=False` if needed.

Update `tests/test_health.py`:

```python
response = await client.get("/health/ready")
assert response.status_code == 503
body = response.json()
assert body["error"] is True
assert body["code"] == "database_unavailable"
assert body["details"] == {"database": "down"}
assert "status" not in body
```

Update `tests/test_tasks_api.py`:

```python
from taskiq.exceptions import SendTaskError

async def failing_kiq() -> FakeTask:
    raise SendTaskError()

monkeypatch.setattr(ping, "kiq", failing_kiq)
...
assert response.status_code == 503
assert response.json()["code"] == "task_enqueue_failed"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `poetry run pytest tests/test_error_mapping.py tests/test_health.py::test_ready_down tests/test_tasks_api.py -v`
Expected: FAIL because SQLAlchemy/Taskiq errors are still `500 internal_error`, health still returns `HealthResponse`, and the task failure is still 500.

- [ ] **Step 3: Implement the infrastructure mappers and handlers**

In `api/errors/mapping.py`:

- `payload_from_sqlalchemy_error` maps `OperationalError`, `InterfaceError`, `DisconnectionError`, and `sqlalchemy.exc.TimeoutError` to `DATABASE_UNAVAILABLE` (503); all other `SQLAlchemyError` including `IntegrityError` to `INTERNAL_ERROR` (500). No details.
- `payload_from_taskiq_error` maps `TaskiqError` to `TASK_ENQUEUE_FAILED` (503). No details.

In `api/errors/handlers.py` add `handle_sqlalchemy_error` and `handle_taskiq_error` and register them for `SQLAlchemyError` and `TaskiqError`. Both use the matching payload and renderer.

In `api/routes/health.py`:

- remove the `Response` parameter and the `responses={503: ...}` declaration;
- remove now-unused `Response`/`status` imports;
- keep `response_model=HealthResponse`;
- on a failed probe raise the `AppError` from the Interfaces block;
- update the docstring to mention the unified 503 envelope.

In `api/routes/tasks.py`, update the `enqueue_ping` docstring: broker failures now
render as `503 task_enqueue_failed`, not `500 internal_error`.

- [ ] **Step 4: Run the focused tests**

Run: `poetry run pytest tests/test_error_mapping.py tests/test_health.py tests/test_tasks_api.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/backend/api/errors/mapping.py src/backend/api/errors/handlers.py src/backend/api/routes/health.py tests/test_error_mapping.py tests/test_health.py tests/test_tasks_api.py
git commit -m "feat: standardize infrastructure error responses"
```

---

### Task 3: Middleware error envelope and allowed hosts

**Files:**
- Create: `src/backend/api/errors/middleware.py`
- Modify: `src/backend/main.py:10-20,131-146`
- Modify: `tests/test_health.py:62-74`
- Create: `tests/test_error_middleware.py`

**Interfaces:**
- Consumes: `render_error_response`, all payload mappers, `resolve_request_id` from Task 1; `BadRequestError` from `core.errors`.
- Produces:
  - `ErrorEnvelopeMiddleware(app: ASGIApp, settings: Settings)`.
  - `AllowedHostsMiddleware(app: ASGIApp, allowed_hosts: Sequence[str], www_redirect: bool = True)`.
  - `create_app` middleware order from innermost to outermost: `AllowedHosts`, `GZip`, `CORS`, `SecurityHeaders`, `ErrorEnvelope`, `RequestContext`.

- [ ] **Step 1: Write the failing middleware tests**

Create `tests/test_error_middleware.py` with:

```python
class RaisingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise BadRequestError(message="middleware failure")


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _middleware_app(settings: Settings) -> Starlette:
    return Starlette(
        routes=[Route("/", _ok)],
        middleware=[
            Middleware(RequestContextMiddleware),
            Middleware(ErrorEnvelopeMiddleware, settings=settings),
            Middleware(RaisingMiddleware),
        ],
    )
```

Tests:

- `test_middleware_error_uses_envelope` — `GET /` on `_middleware_app(settings)` returns 400; body `code == "bad_request"`; body `request_id == response.headers["x-request-id"]`; the header is 32 chars because no incoming ID was supplied.
- `test_error_envelope_is_inside_request_context` — `create_app(settings).user_middleware` has `RequestContextMiddleware` before `ErrorEnvelopeMiddleware`.
- `test_access_log_uses_rendered_status` — call `configure_logging(settings)`, then run `_middleware_app(settings)` under `caplog.at_level(logging.INFO)`; assert at least one captured `caplog.records` entry has `record.msg` dict with `event == "http.request.completed"` and `status_code == 400`.
- `test_disallowed_host_uses_error_envelope` — `create_app(Settings(_env_file=None, allowed_hosts=["api.example.com"], log_json=False))`; request `http://other.example.com/health/live` returns 400; body `code == "bad_request"`; body has `request_id`.
- `test_www_redirect_preserved` — `allowed_hosts=["www.example.com"]`; request `http://example.com/health/live` returns 307 with `location == "http://www.example.com/health/live"`.
- `test_allowed_hosts_passes_non_http_scope` — call `AllowedHostsMiddleware` directly with a `{"type": "websocket"}` scope and assert the inner app was called.

Update `tests/test_health.py::test_live_with_disallowed_host` to assert the same flat envelope as the new middleware test.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `poetry run pytest tests/test_error_middleware.py tests/test_health.py::test_live_with_disallowed_host -v`
Expected: FAIL because the middleware module does not exist and disallowed Host is still plain text.

- [ ] **Step 3: Implement `api/errors/middleware.py`**

`ErrorEnvelopeMiddleware`:

- pass non-HTTP scopes through unchanged;
- wrap the inner app in `try/except` for `AppError`, `StarletteHTTPException`, `SQLAlchemyError`, `TaskiqError`, and `Exception`;
- map with the Task 1/Task 2 payload functions and render with `render_error_response(...)`;
- do not catch `BaseException`/`CancelledError`;
- if the response has already started, let the exception propagate (no streaming responses exist today).

`AllowedHostsMiddleware`:

- pass non-HTTP scopes through unchanged;
- skip validation when `"*"` is in `allowed_hosts`;
- parse Host with `starlette._utils.parse_host_header`; invalid/missing Host raises `BadRequestError(message="Invalid host header")`;
- match exact hosts and `*.example.com` wildcards case-insensitively;
- preserve the old `www_redirect=True` behavior with `RedirectResponse` when the request host is the bare variant of an allowed `www.` host;
- otherwise raise `BadRequestError`.

- [ ] **Step 4: Wire the middleware in `create_app`**

In `src/backend/main.py`:

- replace the `TrustedHostMiddleware` import with `AllowedHostsMiddleware`;
- import `ErrorEnvelopeMiddleware` from `backend.api.errors.middleware`;
- keep this add order: `AllowedHosts`, `GZip`, `CORS`, `SecurityHeaders`, `ErrorEnvelope`, `RequestContext`;
- pass `settings=settings` to `ErrorEnvelopeMiddleware`;
- update the docstring that currently says `RequestContextMiddleware` is the outermost layer to mention that `ErrorEnvelopeMiddleware` sits directly inside it.

- [ ] **Step 5: Run the focused tests**

Run: `poetry run pytest tests/test_error_middleware.py tests/test_health.py tests/test_middleware.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/backend/api/errors/middleware.py src/backend/main.py tests/test_error_middleware.py tests/test_health.py
git commit -m "feat: envelope middleware errors and allowed hosts"
```

---

### Task 4: OpenAPI error contract

**Files:**
- Create: `src/backend/api/errors/openapi.py`
- Modify: `src/backend/api/router.py`
- Modify: `src/backend/api/routes/health.py:8`
- Modify: `src/backend/api/routes/tasks.py:8`
- Create: `tests/test_openapi_errors.py`

**Interfaces:**
- Consumes: `ErrorResponse` from Task 1.
- Produces:
  - `ERROR_RESPONSES: dict[int, dict[str, object]]` for 400, 404, 422, 500, 503.
  - `StandardizedAPIRoute(APIRoute)` merging `ERROR_RESPONSES` with route-specific `responses`.
  - `StandardizedAPIRouter(APIRouter)` defaulting `route_class=StandardizedAPIRoute`.

- [ ] **Step 1: Write the failing OpenAPI tests**

Create `tests/test_openapi_errors.py` with:

```python
class Body(BaseModel):
    value: int


def test_error_response_component(app: FastAPI) -> None:
    components = app.openapi()["components"]["schemas"]
    assert "ErrorResponse" in components
    assert {"error", "code", "message", "description", "request_id"} <= set(
        components["ErrorResponse"]["properties"]
    )
    assert "ErrorCode" in components
    assert "FieldError" in components
    assert "ValidationCode" in components


def test_422_uses_error_response(app: FastAPI) -> None:
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
    operation = app.openapi()["paths"]["/health/live"]["get"]
    for status in ("400", "404", "422", "500", "503"):
        ref = operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorResponse")
```

- [ ] **Step 2: Run the OpenAPI tests to verify they fail**

Run: `poetry run pytest tests/test_openapi_errors.py -v`
Expected: FAIL because `StandardizedAPIRouter` does not exist and routes still use plain `APIRouter`.

- [ ] **Step 3: Implement the route class**

Create `src/backend/api/errors/openapi.py`:

```python
ERROR_RESPONSES: dict[int, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "Bad Request"},
    404: {"model": ErrorResponse, "description": "Not Found"},
    422: {"model": ErrorResponse, "description": "Validation Error"},
    500: {"model": ErrorResponse, "description": "Internal Server Error"},
    503: {"model": ErrorResponse, "description": "Service Unavailable"},
}
```

`StandardizedAPIRoute.__init__` copies `ERROR_RESPONSES`, overlays `kwargs.get("responses") or {}`, assigns the merged dict, then calls `super().__init__`. `StandardizedAPIRouter.__init__` sets `route_class=StandardizedAPIRoute` only when the caller did not supply one.

- [ ] **Step 4: Use the standardized router**

- `src/backend/api/router.py`: `api_router = StandardizedAPIRouter()`.
- `src/backend/api/routes/health.py`: `router = StandardizedAPIRouter(prefix="/health", tags=["health"])`.
- `src/backend/api/routes/tasks.py`: `router = StandardizedAPIRouter(prefix="/tasks", tags=["tasks"])`.
- Remove the now-redundant explicit `responses={503: ...}` only if it remains; health's explicit 503 was removed in Task 2.

- [ ] **Step 5: Run the OpenAPI tests**

Run: `poetry run pytest tests/test_openapi_errors.py -v`
Expected: PASS. `422` references `ErrorResponse`; `HTTPValidationError` is absent from the operation.

- [ ] **Step 6: Run the full suite**

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/backend/api/errors/openapi.py src/backend/api/router.py src/backend/api/routes/health.py src/backend/api/routes/tasks.py tests/test_openapi_errors.py
git commit -m "feat: document unified error responses in openapi"
```

---

### Task 5: Documentation and full verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the completed contract from Tasks 1-4.
- Produces: user-facing documentation of the contract and a clean full verification run.

- [ ] **Step 1: Update `README.md`**

Update the structure tree:

- `api/errors/` — schemas, validation normalization, mapping, handlers, middleware, OpenAPI route class.
- `core/errors.py` — error codes and exceptions.

Update the dependency rule:

- `core.errors` no longer imports `api`; the old note about `core.errors` using `api.schemas` is removed.
- `api.errors` depends on `core.errors` and FastAPI.

Update the Health section:

```text
GET /health/ready — 200 {"status": "ok", "database": "ok"} либо
503 {"error": true, "code": "database_unavailable", "message": "Database is unavailable",
     "description": "База данных временно недоступна",
     "details": {"database": "down"}, "request_id": "<id>"}
```

Add an `## Ошибки API` section after Health with:

- the flat envelope and every field (`error`, `code`, `message`, `description`, `details`, `request_id`);
- the rule that `message` is technical English and `description` is Russian for the client;
- a validation example with `details.fields` and the field codes;
- the statement that 500 never includes `details` in production and `request_id` is the support key;
- the code/status table or a short list of the stable codes;
- the note that `ErrorResponse` is documented in OpenAPI and `422` no longer uses `HTTPValidationError`;
- the note that unknown routes, middleware errors, host errors, readiness, SQLAlchemy, and Taskiq all use the same envelope.

- [ ] **Step 2: Run the full test suite**

Run: `poetry run pytest -q`
Expected: PASS.

- [ ] **Step 3: Run lint and type checks**

Run: `poetry run ruff check .`
Expected: PASS.

Run: `poetry run ruff format --check .`
Expected: PASS. If formatting fails, run `poetry run ruff format .` and re-run the check.

Run: `poetry run mypy src tests`
Expected: PASS.

- [ ] **Step 4: Refresh the graphify graph**

Run: `graphify update .`
Expected: command exits 0.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document standardized api errors"
```

---

## Self-Review Notes

- **Spec coverage:** contract and registry -> Task 1; validation -> Task 1; HTTP/DB/Taskiq/health mapping -> Tasks 1-2; middleware/host -> Task 3; OpenAPI -> Task 4; README/migration -> Task 5.
- **Type consistency:** `ErrorPayload`, `payload_from_*`, `render_error_response`, `resolve_request_id`, `ErrorCode`, `ErrorSpec`, `ERROR_SPECS`, `ValidationCode`, `FieldError`, `ErrorDetails`, `ErrorResponse`, `StandardizedAPIRoute`, `StandardizedAPIRouter`, `ErrorEnvelopeMiddleware`, and `AllowedHostsMiddleware` are used with the same names in every task.
- **Review Focus coverage:** each of the five listed failure modes has a named test in the task that owns the code.
