# AGENTS.md

Инженерные правила и факты проекта Milly backend для coding-агентов.
Продуктовое описание — в `README.md` в корне `backend/`; контракт ошибок API — в `README.md` и `docs/superpowers/specs/2026-11-02-api-error-standardization-design.md`.
Здесь — только конвенции работы с кодом и актуальное состояние проекта.

Обязательный процесс разработки — SOLID+KISS+TDD; докстринги обязательны и поддерживаются в актуальном состоянии.

## 1. Проверка изменений

Перед завершением задачи обязательно выполни:

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
```

Проект работает на Python 3.14, FastAPI 0.156.x, Pydantic 2.x и SQLAlchemy 2.x. Новые runtime/dev-зависимости не добавляй без явной необходимости.

## 2. Границы слоёв

- `src/backend/core/` — конфиг, логирование, общие middleware, реестр ошибок и исключения. `core.errors` не импортирует FastAPI/Starlette и не знает про HTTP.
- `src/backend/api/` — FastAPI-слой: роуты, зависимости, Pydantic-схемы и транспорт ошибок. `api.errors` зависит от `core.errors` и FastAPI.
- `src/backend/db/`, `src/backend/tasks/` — инфраструктура БД и Taskiq.
- `src/backend/main.py` — только сборка приложения; бизнес-логику туда не добавляй.

## 3. Ошибки API

Все ошибки API используют единый плоский конверт:

```json
{"error": true, "code": "not_found", "message": "Resource not found", "description": "Запрошенный ресурс не найден", "details": null, "request_id": "..."}
```

- Все машинные коды — только через `ErrorCode` и `ERROR_SPECS` в `core/errors.py`; строковые коды в роутах запрещены.
- Доменные ошибки поднимай через `AppError` или семантические подклассы. `HTTPException` допустим для HTTP-семантики, но его ответ всё равно проходит через общий маппер; для бизнес-ошибок используй `AppError`.
- `message` — технический английский для логов; `description` — русский текст для клиента.
- `request_id` всегда присутствует и совпадает с заголовком `X-Request-ID`.
- `details` — только безопасные данные. Тела запросов, токены, пароли, SQL, stack trace и Pydantic `input`/`ctx`/`url` в ответ не попадают.
- В production ответ 500 не содержит `details`; в debug допустимы тип и текст исключения.
- Неизвестные маршруты, ошибки middleware, Host, readiness, SQLAlchemy и Taskiq должны проходить через общий рендерер ошибок.
- Новые роутеры создавай через `StandardizedAPIRouter`, чтобы OpenAPI документировал `400`, `404`, `422`, `500`, `503`.
- Порядок middleware не меняй без причины (изнутри наружу): `AllowedHosts` → `GZip` → `CORS` → `SecurityHeaders` → `BodySizeLimit` → `ErrorEnvelope` → `RequestContext`.
- Отклонённый CORS preflight остаётся plain-text `400` от Starlette — это документированное исключение из конверта.

## 4. Тесты ошибок

- Поведение ошибок проверяй в `tests/test_errors.py`, `tests/test_error_mapping.py`, `tests/test_error_middleware.py` и `tests/test_error_validation.py`.
- Для ошибок проверяй HTTP-статус, `code`, `description`, `request_id` и отсутствие утечек в `details`.
- Для 5xx проверяй отсутствие `details` в production; debug-поведение покрывай отдельным тестом.
- Новые коды ошибок и ветки маппинга должны иметь тест на точный статус и текст.

## 5. Докстринги

Докстринги обязательны для всех новых функций, методов и классов. **Докстринг поддерживается в актуальном состоянии**: при изменении сигнатуры, поведения или состава исключений функции обновляй её докстринг в том же изменении. Устаревший или неверный докстринг — такой же дефект, как падающий тест.

Все функции, методы и классы в `src/` уже имеют докстринги; новые и изменяемые функции документируй сразу по шаблону ниже.

- Язык — английский; заголовки секций — как в шаблоне ниже. Русскоязычные докстринги старого кода приводи к шаблону при доработке соответствующего кода.
- Классы — однострочный докстринг: что представляет класс (сущность, DTO, ORM-таблица, исключение, use case, контракт `Protocol`).
- Секции адаптивные: summary + `Purpose` — всегда; `Args`, `Returns`, `Raises` — когда применимо; `Examples`, `Notes` — где добавляют ценность (не нужны у тривиальных `__init__`, property и Protocol-заглушек).
- `Raises` — только реально поднимаемые или явно пробрасываемые исключения; `Examples` — достоверные примеры, согласованные с кодом. Параметры, исключения и поведение не выдумывать.
- В `Notes` отражай границы транзакций («не коммитит; коммит — на вызывающем слое, запись через `flush()`»), сайд-эффекты, идемпотентность, поля, генерируемые БД.
- Методы `Protocol`/ABC-заглушек описывают контракт, который обязан выполнить реализатор; `Args`/`Returns` для них обязательны.

```python
def calculate_order_total(items: list[dict], discount_percent: float = 0) -> float:
    """
    Calculate the total order cost.

    Purpose:
        Sums item costs and applies the discount; used when generating receipts.

    Args:
        items: Order items, each a dict with "price" (float) and "quantity" (int).
        discount_percent: Discount from 0 to 100. Defaults to 0.

    Returns:
        float: Total cost rounded to 2 decimal places.

    Raises:
        ValueError: If `items` is empty or a price/quantity/percent is out of range.

    Examples:
        >>> calculate_order_total([{"price": 100.0, "quantity": 2}], discount_percent=10)
        180.0

    Notes:
        - Discount is applied before rounding; no side effects.
    """
```

## 6. graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
