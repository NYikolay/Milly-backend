# Архитектура стандартизации ошибок API

- Дата: 2026-11-02
- Статус: черновик на согласование
- Область: `backend/` (FastAPI, Python 3.14)
- Потребитель контракта: React-приложение

## 1. Зачем это нужно

Сейчас ошибки в Milly уже частично унифицированы, но единый контракт
нарушается:

- неизвестный маршрут отдаёт `http_404`, а `NotFoundError` — `not_found`;
- ошибка из middleware может стать `500 internal_error`, даже если это
  ожидаемая доменная ошибка;
- `503` readiness и ошибка `TrustedHostMiddleware` обходят конверт;
- `details` валидации — сырой список Pydantic с `input`, `ctx` и `url`;
- `ErrorResponse` не описан в OpenAPI, а для `422` документируется
  `HTTPValidationError`, которого в рантайме нет;
- коды ошибок — строковые литералы, разбросанные по коду.

Цель — один предсказуемый контракт для всех ошибок API, чтобы новый
эндпоинт не мог случайно вернуть ошибку в другом формате, а React-клиент
обрабатывал ошибки одинаково во всём приложении.

## 2. Цели

1. Один JSON-конверт для любой ошибки API: 400, 401, 403, 404, 405, 409,
   413, 415, 422, 429, 500, 503 и доменных ошибок.
2. Стабильный машинный `code` из централизованного реестра.
3. Отдельно техническое сообщение для логов (`message`) и безопасный
   русский текст для пользователя (`description`).
4. Единый формат ошибок валидации с привязкой к полю, без эха входных
   данных.
5. Один `request_id` для корреляции ответа, заголовка `X-Request-ID` и
   логов; для 500 это ключ обращения в поддержку.
6. Одинаковое поведение для ошибок из роутов, зависимостей, middleware,
   БД и брокера задач.
7. `ErrorResponse` в OpenAPI для всех задокументированных ошибок, включая
   `422` вместо стандартного `HTTPValidationError`.
8. Расширяемость: новая доменная ошибка добавляется в одном месте без
   изменения рендерера и обработчиков.

## 3. Не-цели

- Не менять формат успешных ответов.
- Не внедрять RFC 9457 / `application/problem+json`: выбран собственный
  конверт, совместимый по духу с примером из соседнего проекта.
- Не делать полноценную i18n с `Accept-Language`; русские `description`
  фиксированы в коде. Интерфейс для будущей локализации оставляем, но не
  реализуем.
- Не добавлять auth/permissions, Sentry/OpenTelemetry, DLQ и retry-политики.
- Не превращать все исключения БД в бизнес-ошибки. Глобальный маппинг —
  страховка; бизнес-код должен явно поднимать `AppError`.

## 4. Критерии неудачи

- Хотя бы один путь возвращает ошибку вне конверта.
- `ErrorResponse` в OpenAPI не совпадает с рантаймом.
- 500 в production раскрывает текст исключения, stack trace или SQL.
- Ошибки валидации возвращают `input`, `ctx` или другие сырые данные.
- Новый эндпоинт требует ручного копирования обработчиков ошибок.
- Коды ошибок снова появляются строковыми литералами в роутах.
- `core` зависит от `api`.

## 5. Контракт ответа

Плоский конверт:

```json
{
  "error": true,
  "code": "validation_error",
  "message": "Request validation failed",
  "description": "Проверьте правильность заполнения полей",
  "details": {
    "fields": [
      {
        "field": "body.email",
        "code": "invalid_format",
        "description": "Некорректный адрес электронной почты"
      }
    ]
  },
  "request_id": "4f8c2f5d9a7b4c0e8d1f3a6b2c9e7d40"
}
```

### 5.1 Поля

| Поле | Тип | Обязательно | Назначение |
|---|---|---|---|
| `error` | `true` | да | Явный признак ошибки для клиента и TypeScript-дискриминации. |
| `code` | `ErrorCode` | да | Стабильный машинный код. Не меняется при изменении текста. |
| `message` | `string` | да | Короткое техническое сообщение на английском для логов и разработчиков. Клиент его не показывает. |
| `description` | `string` | да | Безопасный русский текст для пользователя. |
| `details` | `object` | нет | Структурированные детали, форма зависит от `code`; всегда JSON-объект. |
| `request_id` | `string` | да | ID запроса; совпадает с `X-Request-ID`. |

Правила:

- `details` не сериализуется, если равен `null` (используется
  `exclude_none`).
- `message` не содержит пользовательских данных, SQL, путей и stack trace.
- `description` не содержит технических деталей и всегда пригоден для
  показа пользователю.
- `request_id` генерируется, если middleware не успел его создать. В
  ответе и заголовке всегда одно и то же значение.
- `error_id` из примера соседнего проекта не вводится: его роль выполняет
  `request_id`, уже существующий в Milly и проброшенный в Taskiq.

### 5.2 Ошибки валидации

`details.fields` — список объектов:

| Поле | Тип | Назначение |
|---|---|---|
| `field` | `string` | Путь к полю: `body.email`, `query.limit`, `path.id`, `body.items.0.name`. |
| `code` | `ValidationCode` | Стабильный код причины: `required`, `invalid_type`, `invalid_format`, `too_short`, `too_long`, `out_of_range`, `invalid_value`, `invalid_json`. |
| `description` | `string` | Русский текст для пользователя. |

Правила:

- `input`, `ctx`, `url` и `msg` Pydantic в ответ не попадают.
- `field` собирается из `loc` через точку, включая префикс источника
  (`body`, `query`, `path`).
- Неизвестный тип ошибки Pydantic нормализуется в `invalid_value` с
  безопасным текстом.
- Некорректный JSON (`json_invalid`) возвращает `400 bad_request`, а не
  `422`: тело запроса не удалось разобрать.
- Остальные ошибки валидации возвращают `422 validation_error`.

Пример:

```json
{
  "error": true,
  "code": "validation_error",
  "message": "Request validation failed",
  "description": "Проверьте правильность заполнения полей",
  "details": {
    "fields": [
      {
        "field": "body.value",
        "code": "invalid_type",
        "description": "Некорректный тип значения"
      }
    ]
  },
  "request_id": "2b7e151628aed2a6abf7158809cf4f3c"
}
```

### 5.3 Серверная ошибка

```json
{
  "error": true,
  "code": "internal_error",
  "message": "Internal server error",
  "description": "Внутренняя ошибка сервера",
  "request_id": "2b7e151628aed2a6abf7158809cf4f3c"
}
```

`details` отсутствует в production всегда. В локальном debug-режиме
допускается `details` с типом и текстом исключения; это не должно попадать
в production.

### 5.4 Доменная ошибка

```json
{
  "error": true,
  "code": "conflict",
  "message": "Email is already registered",
  "description": "Этот email уже зарегистрирован",
  "details": {
    "field": "email"
  },
  "request_id": "2b7e151628aed2a6abf7158809cf4f3c"
}
```

`details` — только безопасные данные. Пароли, токены, тела запросов и
персональные данные туда не добавляются.

## 6. Реестр кодов

`ErrorCode` — `StrEnum` в `backend/core/errors.py`. Значение —
стабильная строка в `snake_case`.

| Код | HTTP | Уровень лога | `description` по умолчанию |
|---|---:|---|---|
| `bad_request` | 400 | warning | Некорректный запрос |
| `validation_error` | 422 | info | Проверьте правильность заполнения полей |
| `unauthorized` | 401 | warning | Требуется авторизация |
| `forbidden` | 403 | warning | Недостаточно прав |
| `not_found` | 404 | info | Запрошенный ресурс не найден |
| `method_not_allowed` | 405 | warning | Метод не поддерживается |
| `conflict` | 409 | warning | Операция конфликтует с текущим состоянием |
| `payload_too_large` | 413 | warning | Слишком большой запрос |
| `unsupported_media_type` | 415 | warning | Неподдерживаемый тип содержимого |
| `rate_limited` | 429 | warning | Слишком много запросов |
| `internal_error` | 500 | error | Внутренняя ошибка сервера |
| `not_implemented` | 501 | warning | Функция не реализована |
| `service_unavailable` | 503 | error | Сервис временно недоступен |
| `database_unavailable` | 503 | error | База данных временно недоступна |
| `task_enqueue_failed` | 503 | error | Не удалось поставить задачу в очередь |
| `http_error` | из исключения | warning | Ошибка запроса |

`ErrorSpec` содержит `status_code`, `log_level`, `message` (техническое
сообщение по умолчанию) и `description`. `ERROR_SPECS` — словарь
`ErrorCode -> ErrorSpec`; тест проверяет, что для каждого члена enum есть
спецификация.

`http_error` — единственное исключение из правила «статус берётся из
реестра»: он используется только адаптером `HTTPException` для неизвестного
HTTP-статуса, и статус берётся из исключения. `AppError` в этом случае не
создаётся: адаптер передаёт код и явный статус напрямую в рендерер.

### 6.1 Как добавить доменную ошибку

1. Добавить код в `ErrorCode` и `ERROR_SPECS`.
2. Создать подкласс `AppError` (или существующего семантического класса) с
   этим кодом.
3. Поднимать его в доменном/прикладном слое.
4. Добавить тест на контракт.

Пример:

```python
class EmailAlreadyRegisteredError(ConflictError):
    _code = ErrorCode.EMAIL_ALREADY_REGISTERED

    def __init__(self, email: str) -> None:
        super().__init__(
            message="Email is already registered",
            description="Этот email уже зарегистрирован",
            details={"email": email},
        )
```

## 7. Маппинг внешних ошибок

### 7.1 `HTTPException` Starlette/FastAPI

- 400 -> `bad_request`
- 401 -> `unauthorized`
- 403 -> `forbidden`
- 404 -> `not_found`
- 405 -> `method_not_allowed`
- 409 -> `conflict`
- 413 -> `payload_too_large`
- 415 -> `unsupported_media_type`
- 422 -> `validation_error`
- 429 -> `rate_limited`
- 500 -> `internal_error`
- 501 -> `not_implemented`
- 503 -> `service_unavailable`
- остальные -> `http_error` со статусом из исключения

Правила:

- Для 4xx строковый `detail` становится `message`; `description` берётся
  из реестра. Такой `detail` считается разработческим сообщением: код,
  который его поднимает, обязан не передавать туда секреты и пользовательские
  данные (глобально вычистить произвольную строку невозможно).
- Для 4xx нестроковый `detail` сохраняется в `details` как
  `{"detail": ...}` без `str()`-преобразования.
- Для 5xx `detail` никогда не попадает в ответ: `message` и `description`
  берутся из реестра, а исходный `detail` пишется в лог. В debug-режиме
  5xx может содержать `details` с типом и текстом исключения; это локальное
  поведение, в production таких данных нет.
- Заголовки исключения (`WWW-Authenticate`, `Allow`, `Retry-After`)
  пробрасываются в ответ.
- Неизвестный маршрут и явный `NotFoundError` дают один код `not_found`.

### 7.2 Валидация

- `RequestValidationError` -> `422 validation_error` или `400 bad_request`
  для `json_invalid`.
- `ResponseValidationError` -> `500 internal_error`: это баг сервера, а не
  клиента. В production детали не раскрываются; в debug допускаются общие
  debug-details, как для любого 500.

### 7.3 SQLAlchemy

- `OperationalError`, `InterfaceError`, `DisconnectionError`,
  `sqlalchemy.exc.TimeoutError` -> `503 database_unavailable`.
- Остальные `SQLAlchemyError`, включая `IntegrityError` -> `500
  internal_error`.
- Бизнес-конфликты (например, уникальный email) должны явно
  преобразовываться в `ConflictError` в прикладном слое; глобальный
  маппинг не угадывает бизнес-смысл.

### 7.4 Taskiq

- `TaskiqError` -> `503 task_enqueue_failed`.
- `details` может содержать `{"task": "<name>"}`; без внутренних деталей
  брокера.
- Неизвестное исключение брокера остаётся `500 internal_error`.

### 7.5 Health readiness

`GET /health/ready` при недоступной БД поднимает
`AppError(ErrorCode.DATABASE_UNAVAILABLE, details={"database": "down"})`.
Ответ 503 теперь использует общий конверт. Успешный ответ 200 не меняется.

### 7.6 Некорректный Host

Штатный `TrustedHostMiddleware` возвращает plain-text 400 в обход
конверта. Он заменяется на `AllowedHostsMiddleware`, который проверяет Host
и поднимает `BadRequestError`; внешний `ErrorEnvelopeMiddleware` рендерит
стандартный ответ.

## 8. Слои и зависимости

Целевая структура:

```text
src/backend/
├── core/
│   └── errors.py                # ErrorCode, ErrorSpec, AppError и подклассы
├── api/
│   ├── errors/
│   │   ├── __init__.py
│   │   ├── schemas.py           # FieldError, ErrorDetails, ErrorResponse
│   │   ├── validation.py        # ValidationCode, нормализация Pydantic
│   │   ├── mapping.py           # HTTPException, SQLAlchemy, Taskiq -> AppError
│   │   ├── handlers.py          # рендерер и регистрация exception handlers
│   │   ├── middleware.py        # ErrorEnvelopeMiddleware, AllowedHostsMiddleware
│   │   └── openapi.py           # StandardizedAPIRoute/Router
│   ├── schemas.py               # только успешные схемы
│   └── routes/
└── main.py                      # сборка middleware и регистрация handlers
```

Правило зависимостей:

- `core.errors` не импортирует FastAPI/Starlette и не знает про HTTP-слой.
- `api.errors` зависит от `core.errors` и FastAPI.
- `main.py` собирает всё вместе.
- `core` больше не импортирует `api.schemas`; текущее нарушение
  устраняется.

## 9. Исключения

```python
class AppError(Exception):
    code: ErrorCode
    message: str
    description: str
    details: Mapping[str, object] | None
    headers: Mapping[str, str] | None
    log_level: int
```

- `AppError` не содержит `status_code`: статус определяется реестром.
  Единственное исключение — адаптер `HTTPException` с неизвестным статусом:
  он не создаёт `AppError`, а передаёт статус в рендерер напрямую.
- `message` по умолчанию берётся из `ErrorSpec.message`; `description` — из
  `ErrorSpec.description`.
- `details` и `headers` копируются в экземпляр; вызывающий код не может
  изменить их после создания.
- `log_level` можно переопределить для конкретного случая.
- Семантические подклассы: `BadRequestError`, `UnauthorizedError`,
  `ForbiddenError`, `NotFoundError`, `ConflictError`, `RateLimitedError`,
  `ServiceUnavailableError`.
- Доменные подклассы переопределяют `_code` и передают свои сообщения.

## 10. Рендерер и обработчики

`api/errors/handlers.py`:

- `_render_error(...)` — единственное место сборки `ErrorResponse`.
- Добавляет security headers, CORS headers (если ответ обходит
  `CORSMiddleware`), `X-Request-ID` и заголовки исключения.
- Нормализует `details` через `jsonable_encoder`; если сериализация
  невозможна, детали отбрасываются, а факт пишется в лог.
- Пишет структурированный лог `api.error` с `error_code`, `status_code`,
  `request_id`, `method`, `path`; для 5xx — с traceback.
- Для 4xx не логирует тело запроса и `details`.

Регистрируются обработчики:

- `AppError`
- `RequestValidationError`
- `ResponseValidationError`
- `StarletteHTTPException`
- `SQLAlchemyError`
- `TaskiqError`
- `Exception` (последний рубеж)

## 11. Middleware

### 11.1 `ErrorEnvelopeMiddleware`

Располагается снаружи всех middleware, кроме `RequestContextMiddleware`.
Нужен потому, что exception handlers FastAPI живут внутри
`ExceptionMiddleware`: ошибка, поднятая пользовательским middleware, иначе
станет `500 internal_error`.

- Ловит `AppError`, `HTTPException`, `SQLAlchemyError`, `TaskiqError` и
  неизвестные `Exception`.
- Использует те же мапперы и рендерер, что и handlers.
- Не ловит `BaseException`/`CancelledError`.
- Если ответ уже начал отправляться, не пытается его переписать; это
  ограничение документируется (стриминговых ответов сейчас нет).

Порядок middleware в `create_app` (изнутри наружу):

```text
AllowedHosts
GZip
CORS
SecurityHeaders
ErrorEnvelope
RequestContext
```

`add_middleware` добавляет слой наружу, поэтому `RequestContextMiddleware`
регистрируется последним и остаётся самым внешним, а
`ErrorEnvelopeMiddleware` — непосредственно под ним. Так access log видит
фактический статус ответа, который отрендерил `ErrorEnvelopeMiddleware`, а
не промежуточный 500. ID запроса уже лежит в `scope.state` к моменту
работы обработчика ошибок. Если ошибка возникнет в самом
`RequestContextMiddleware`, её обработает запасной `Exception` handler
Starlette.

### 11.2 `AllowedHostsMiddleware`

- Повторяет правила `TrustedHostMiddleware`: `*`, точное совпадение,
  wildcard `*.example.com`; порт отбрасывается.
- При некорректном Host поднимает `BadRequestError`.
- При `allowed_hosts == ["*"]` не устанавливается.
- Сохраняет поведение `www_redirect=True`: если Host совпадает с
  `www.`-вариантом разрешённого, возвращается redirect, как у
  `TrustedHostMiddleware`.
- Для не-HTTP scope, включая WebSocket, пропускает запрос без изменений:
  JSON-конверт к WebSocket неприменим. При появлении WS-маршрутов
  потребуется отдельное решение.

## 12. OpenAPI

- `ErrorResponse`, `FieldError`, `ErrorDetails`, `ErrorCode`,
  `ValidationCode` попадают в components.
- `ErrorDetails` — открытая модель: известное поле `fields` для
  валидации, остальные ключи разрешены (`additionalProperties: true`).
  Это позволяет разным кодам иметь свои безопасные детали без расхождения
  OpenAPI и рантайма.
- `StandardizedAPIRoute` автоматически добавляет к каждому роуту ответы
  `400`, `404`, `422`, `500`, `503` с моделью `ErrorResponse`.
- Явное наличие `422` в `responses` отключает автоматический
  `HTTPValidationError` FastAPI.
- Роуты с дополнительными ошибками (`401`, `403`, `409`, `429`)
  добавляют их через `responses`; описания общих ошибок не дублируются.
- `StandardizedAPIRouter` задаёт `route_class` по умолчанию, чтобы новые
  роутеры получали поведение автоматически.
- В схему добавляются примеры для validation, not_found, internal_error.

## 13. Наблюдаемость

- Один `request_id` на запрос: ответ, `X-Request-ID`, structlog и Taskiq
  labels.
- Лог ошибки: `api.error` с `error_code`, `status_code`, `request_id`,
  `method`, `path`.
- 4xx логируются на уровне из `ErrorSpec` (обычно info/warning), без
  traceback и без `details`.
- 5xx логируются с `exc_info`; ответ не содержит деталей в production.
- Access log `http.request.completed` сохраняется; для 500 будет и access
  log, и `api.error`, что допустимо: один фиксирует факт запроса, второй —
  причину.
- `request_id` остаётся ключом для поиска в логах; отдельный `error_id` не
  нужен.

## 14. Безопасность

- 500 не раскрывает exception message, stack trace, SQL и имена таблиц.
- 5xx `HTTPException.detail` никогда не попадает в ответ; он пишется
  только в лог.
- Для 4xx строковый `detail` — разработческое сообщение; вызывающий код
  обязан не передавать туда секреты и пользовательские данные.
- Ошибки валидации не возвращают `input`, `ctx`, `url`; это исключает эхо
  паролей и токенов.
- `details` доменных ошибок — ответственность вызывающего кода; в
  docstring указывается запрет на секреты.
- Заголовки исключений пробрасываются, но не добавляются произвольно.
- В debug-режиме детали 500 допустимы только локально; тест проверяет
  production-поведение.

## 15. Тестирование (TDD)

Сначала пишутся падающие тесты, затем реализация.

### 15.1 Контракт

- 404 неизвестного маршрута: `error=true`, `code="not_found"`,
  `description`, `request_id`.
- 405: `method_not_allowed`, заголовок `Allow` сохранён.
- 401/403: соответствующие коды; `WWW-Authenticate` сохранён.
- 409: `conflict`.
- 422: `validation_error`, `details.fields` — список, нет `input`/`ctx`.
- Некорректный JSON: 400 `bad_request`, field code `invalid_json`.
- 500: `internal_error`, нет `details` в production; `request_id` есть.
- 500 в debug: `details` с типом и текстом исключения; в production этих
  данных нет.
- `AppError` с доменным кодом: статус из реестра, кастомные
  `description`/`details`/`headers`.
- `ResponseValidationError`: 500 `internal_error`.
- `HTTPException` с нестроковым `detail` на 4xx: структура сохранена в
  `details`.
- `HTTPException(500, detail="secret")`: `detail` не попадает в ответ.
- `HTTPException` с неизвестным статусом: статус сохранён, код
  `http_error`.
- `request_id` в теле совпадает с `X-Request-ID`; при отсутствии входящего
  заголовка генерируется.

### 15.2 Все пути ошибок

- Ошибка из пользовательского middleware (`AppError`, `HTTPException`) —
  правильный конверт, не 500.
- Access log для ошибки из middleware содержит фактический статус ответа,
  а не промежуточный 500.
- Некорректный Host — конверт `bad_request`, не plain text.
- `www`-redirect для разрешённого Host сохраняется; WebSocket scope
  проходит без изменений.
- `/health/ready` при недоступной БД — 503 `database_unavailable` с
  `details.database="down"`.
- Ошибка Taskiq — 503 `task_enqueue_failed`.
- `OperationalError` БД — 503 `database_unavailable`.
- Неизвестное исключение — 500 `internal_error`, traceback в логах.

### 15.3 OpenAPI

- `ErrorResponse` присутствует в components.
- `422` ссылается на `ErrorResponse`, а не на `HTTPValidationError`.
- У роутов есть задокументированные `400`, `404`, `422`, `500`, `503`.
- `ErrorCode` и `ValidationCode` — enum в схеме.
- `ErrorDetails` содержит `fields` и разрешает дополнительные ключи.

### 15.4 Реестр и логи

- Для каждого `ErrorCode` есть `ErrorSpec`.
- Нет дублирующихся значений `ErrorCode`.
- Лог 4xx содержит `error_code`/`status_code`/`request_id` и не содержит
  `details`.
- Лог 5xx содержит traceback.
- Секреты из входных данных не попадают в ответ и лог.

### 15.5 Проверки проекта

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy src tests
```

## 16. Совместимость и миграция

- API находится в начале разработки, внешних клиентов нет; смена формата
  ошибок выполняется без версионирования.
- Успешные ответы не меняются.
- `GET /health/ready` меняет тело 503 на общий конверт; Docker healthcheck
  использует статус-код и не ломается.
- README обновляется: структура, контракт ошибок, коды, примеры,
  поведение 500 и readiness.
- Тесты, ожидающие `http_404`, `error.code` внутри вложенного `error` или
  503 `HealthResponse`, обновляются.

## 17. Принятые решения

1. Плоский конверт, `error: true` сохраняется для явной дискриминации.
2. `message` — технический английский, `description` — пользовательский
   русский; это разделение из примера соседнего проекта.
3. `request_id` вместо отдельного `error_id`: один ID уже есть в Milly и
   пробрасывается в фоновые задачи.
4. `details` всегда объект; ошибки валидации лежат в `details.fields`.
5. `details.fields[].code` — нормализованный `ValidationCode`, а не сырой
   `type` Pydantic; `description` — русский текст.
6. Коды и статусы — в центральном реестре; строковые литералы в роутах
   запрещены.
7. `AppError` не знает HTTP-статус; статус определяет реестр.
8. Глобальный маппинг SQLAlchemy/Taskiq — страховка; бизнес-смысл
   преобразуется в `AppError` явно.
9. Readiness 503 и некорректный Host стандартизируются, исключений из
   конверта не остаётся.
10. OpenAPI описывает `ErrorResponse` через кастомный route class, а не
    через ручное дублирование в каждом роуте.
11. Для 5xx `HTTPException.detail` никогда не попадает в ответ; для 4xx
    строковый `detail` считается безопасным разработческим сообщением.
12. Адаптер `HTTPException` с неизвестным статусом не создаёт `AppError`,
    а передаёт статус в рендерер напрямую.
13. `ErrorDetails` — открытый объект с известным `fields`; это сохраняет
    совпадение OpenAPI и рантайма.
14. `RequestContextMiddleware` остаётся самым внешним, чтобы access log
    видел фактический статус ошибки из middleware.
15. `AllowedHostsMiddleware` сохраняет `www`-redirect; WebSocket scope
    проходит без изменений, пока WS-маршрутов нет.

## 18. Риски

| Риск | Митигация |
|---|---|
| `ErrorEnvelopeMiddleware` перехватит слишком много и скроет баг | Ловим только `Exception`; неизвестное всегда 500 с traceback; тесты на middleware-ошибки. |
| Добавление `ErrorCode` — изменение OpenAPI enum | Изменения только аддитивные; клиент обязан игнорировать неизвестный код. |
| Русские тексты захардкожены | Осознанно; будущая i18n потребует отдельного решения, контракт поля `description` не изменится. |
| 503 readiness изменит ожидания эксплуатации | Docker healthcheck смотрит только статус; README и тесты обновляются. |
| Дублирование access log и `api.error` для 500 | Разные назначения; при необходимости access log можно дополнить `error_code` позже. |
| Ошибка рендеринга `details` | `jsonable_encoder` + fallback без деталей; тест на несериализуемые данные. |

## 19. Порядок реализации

1. Реестр `ErrorCode`/`ErrorSpec` и исключения + тесты реестра.
2. Схемы конверта и нормализация валидации + контрактные тесты.
3. Рендерер и exception handlers + тесты всех статусов.
4. `ErrorEnvelopeMiddleware` и `AllowedHostsMiddleware` + тесты
   middleware-ошибок.
5. `StandardizedAPIRoute/Router` и OpenAPI-тесты.
6. Перевод health и tasks на общий контракт.
7. Обновление README и полный прогон pytest/ruff/mypy.
