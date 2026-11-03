# Milly Backend

Production-скелет backend-сервиса Milly: FastAPI, асинхронный SQLAlchemy 2
(asyncpg), Alembic, PostgreSQL за PgBouncer, NATS JetStream + taskiq для
фоновых задач, Redis как result backend и structlog для логирования.
Приложение и инфраструктура запускаются через Docker Compose.

## Структура

```text
backend/
├── alembic/                 # env.py и ревизии Alembic
├── src/backend/
│   ├── main.py              # app factory, lifespan, middleware
│   ├── api/                 # роутеры, зависимости и Pydantic-схемы
│   │   ├── errors/          # схемы, нормализация валидации, маппинг,
│   │   │                    # обработчики, middleware, OpenAPI-класс роутов
│   │   └── routes/          # health.py, tasks.py, auth.py
│   ├── admin/               # SQLAdmin: вьюхи, вход, монтирование
│   ├── auth/                # initData, JWT, refresh-сессии и cookie
│   ├── core/                # настройки, логирование, middleware
│   │   └── errors.py        # коды ошибок и исключения
│   ├── db/                  # engine/session, declarative base, readiness
│   ├── models/              # User, TelegramProfile, AuthSession
│   └── tasks/               # broker, worker, scheduler, примеры задач
├── tests/                   # unit-тесты без инфраструктуры
├── compose.yml              # production-подобный стек
├── compose.override.yml     # dev-режим (подхватывается автоматически)
├── Dockerfile               # multi-stage: builder → dev / runtime
└── .env.example             # шаблон локального окружения
```

Правило зависимостей: `main.py` собирает приложение и подключает роутеры;
`api` и `auth` зависят от `core`, `db` и `models`; `tasks` — от `db` и
`core`; `db` — от `core`. `auth` не импортирует `api`. `api.errors`
зависит от `core.errors` и FastAPI; `core.errors` не импортирует `api`.

## Быстрый старт

Требуется Docker с Compose. Из каталога `backend/`:

```bash
cp -n .env.example .env   # -n сохраняет уже существующий .env
docker compose up -d --build --wait
curl -fsS http://localhost:8000/health/live
```

`cp -n` не перезаписывает существующий `.env` (например, с локальными
`PROVIDER_*`). `.env` не коммитится.

## Dev и production-подобный режимы

- **Dev** — `docker compose up -d --build`. Автоматически применяется
  `compose.override.yml`: API/worker/scheduler собираются из target `dev`,
  исходники и `alembic/` монтируются в контейнеры, API работает с
  `--reload`, включены `/docs`, console-логи и опубликованы порты
  `8000`, `5432`, `6432`, `4222`, `8222`, `6380` (Redis).
- **Production-подобный** — `docker compose -f compose.yml up -d --build`.
  Используется только базовый файл: runtime-образ под non-root, docs
  отключены, логи JSON, наружу опубликован только `8000`. One-shot
  сервис `migrate` применяет миграции до запуска API, worker и scheduler.
- Остановка: `docker compose down` или
  `docker compose -f compose.yml down`. Флаг `-v` удаляет volumes с
  данными PostgreSQL, NATS и Redis.

## Health

- `GET /health/live` — liveness процесса, не обращается к зависимостям:
  `200 {"status": "ok"}`.
- `GET /health/ready` — readiness с проверкой PostgreSQL через PgBouncer:

  ```text
  GET /health/ready — 200 {"status": "ok", "database": "ok"} либо
  503 {"error": true, "code": "database_unavailable", "message": "Database is unavailable",
       "description": "База данных временно недоступна",
       "details": {"database": "down"}, "request_id": "<id>"}
  ```
- Docker healthcheck API использует `/health/ready`.

## Ошибки API

Все ошибки API возвращаются в едином плоском конверте: неизвестные маршруты,
ошибки middleware и проверки Host, readiness, SQLAlchemy и Taskiq используют
одну и ту же схему.

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
  "request_id": "<id>"
}
```

- `error` — всегда `true`;
- `code` — стабильный машиночитаемый код ошибки;
- `message` — техническое сообщение на английском для логов и разработчиков;
- `description` — описание для клиента на русском;
- `details` — необязательные структурированные детали; поле отсутствует, если
  деталей нет;
- `request_id` — идентификатор запроса, совпадает с заголовком `X-Request-ID`.

Ошибки валидации возвращают `422`; нормализованные ошибки полей находятся в
`details.fields`, каждый элемент содержит `field` (путь с точками), `code` и
русскоязычный `description`. Некорректный JSON возвращает `400` с кодом
`bad_request`.

Коды полей: `required`, `invalid_type`, `invalid_format`, `too_short`,
`too_long`, `out_of_range`, `invalid_value`, `invalid_json`. Входные
значения и сообщения Pydantic в ответ не попадают.

В production ответ `500` никогда не содержит `details`. Для диагностики
используйте `request_id`: это ключ поддержки, по которому находятся связанные
записи в логах. В debug-режиме `details` может содержать тип и сообщение
исключения.

Единственное документированное исключение из конверта — отклонённый CORS
preflight: `CORSMiddleware` отвечает plain-text `400` (`Disallowed CORS
origin`) до рендеринга ошибок. Это протокольный handshake браузера, и
прикладной JS не читает его тело; поведение закреплено тестом.

Стабильные коды ошибок:

| `code` | HTTP | Значение |
| --- | --- | --- |
| `bad_request` | 400 | некорректный запрос, включая malformed JSON |
| `validation_error` | 422 | ошибка валидации полей |
| `unauthorized` | 401 | требуется авторизация |
| `forbidden` | 403 | недостаточно прав |
| `not_found` | 404 | ресурс не найден |
| `method_not_allowed` | 405 | метод не поддерживается |
| `conflict` | 409 | конфликт с текущим состоянием |
| `payload_too_large` | 413 | слишком большой запрос |
| `unsupported_media_type` | 415 | неподдерживаемый тип содержимого |
| `rate_limited` | 429 | слишком много запросов |
| `internal_error` | 500 | внутренняя ошибка сервера |
| `not_implemented` | 501 | функция не реализована |
| `auth_not_configured` | 503 | не заданы обязательные настройки аутентификации |
| `service_unavailable` | 503 | сервис временно недоступен |
| `database_unavailable` | 503 | база данных временно недоступна |
| `task_enqueue_failed` | 503 | не удалось поставить задачу в очередь |
| `http_error` | исходный | прочий HTTP-статус без отдельного кода |

В OpenAPI для каждой операции документируется схема `ErrorResponse` для
ответов `400`, `404`, `422`, `500` и `503`; для `422` больше не используется
`HTTPValidationError`. Если операция возвращает специфичные ошибки (`401`,
`403`, `409`, `503` и т.п.), они документируются отдельно: статус берётся из
`ERROR_SPECS`, а описание начинается с машинного `code` и поясняет, когда
именно операция его возвращает. Например, для `POST /api/v1/auth/telegram`
`403` описан как `forbidden`, а `503` — как `auth_not_configured`.

## Аутентификация Telegram Mini App

Вход выполняется по подписанным данным запуска Mini App
(`Telegram.WebApp.initData`). Backend проверяет HMAC-SHA256 с секретом,
производным от bot token, свежесть `auth_date` и отсутствие `is_bot=true`.
`initDataUnsafe` не доверяется и для авторизации не используется; сырые init
data и токены не логируются.

Flow:

1. `POST /api/v1/auth/telegram` с `{"init_data": "<raw initData>"}` —
   валидирует данные, создаёт или обновляет `User` и `TelegramProfile` и
   возвращает access-токен. Refresh-токен приходит только в HttpOnly-cookie.
2. Access-токен (JWT HS256, TTL 900 секунд плюс 30-секундный leeway при
   проверке — фактически около 15 минут) клиент хранит только в памяти и
   передаёт в `Authorization: Bearer`.
3. `POST /api/v1/auth/refresh` без тела — читает refresh из cookie, ротирует
   его и возвращает новый access; cookie перезаписывается.
4. `POST /api/v1/auth/logout` без тела — отзывает семейство refresh-токенов и
   очищает cookie; ответ `204`.
5. `GET /api/v1/auth/me` с `Authorization: Bearer <access>` — возвращает
   пользователя с Telegram-профилем.

```text
POST /api/v1/auth/telegram
{"init_data": "query_id=...&user=...&auth_date=...&hash=..."}
200 {"access_token": "<jwt>", "token_type": "bearer", "expires_in": 900,
     "user": {"id": 1, "created_at": "...", "telegram_profile": {"..."}}}

POST /api/v1/auth/refresh
200 {"access_token": "<jwt>", "token_type": "bearer", "expires_in": 900}
Set-Cookie: milly_refresh=<opaque>; HttpOnly; Secure; SameSite=Lax;
            Path=/api/v1/auth; Max-Age=2592000

POST /api/v1/auth/logout
204

GET /api/v1/auth/me
Authorization: Bearer <jwt>
200 {"id": 1, "created_at": "...",
     "telegram_profile": {"telegram_id": 123, "first_name": "Ivan", "..."}}
```

Refresh-токен непрозрачный; в БД хранится только его SHA-256 хеш. Cookie
имеет параметры:

| Параметр | Значение |
| --- | --- |
| Имя | `AUTH_REFRESH_COOKIE_NAME` (по умолчанию `milly_refresh`) |
| `HttpOnly` | да, JavaScript не может прочитать refresh |
| `Secure` | `AUTH_REFRESH_COOKIE_SECURE`; `true` в production |
| `SameSite` | `AUTH_REFRESH_COOKIE_SAMESITE`, по умолчанию `lax` |
| `Path` | `"{API_V1_STR}/auth"` |
| `Max-Age` | `AUTH_REFRESH_TTL_SECONDS` (30 дней) |

Refresh строго ротируется: повтор старого токена вне 30-секундного
grace-окна отзывает всё семейство. Ошибки refresh не очищают cookie, logout
очищает. Access после logout не отзывается мгновенно и остаётся валидным до
`exp` — около 15 минут (900 секунд TTL плюс 30-секундный leeway).

Контракт рассчитан на same-site (или same-origin) frontend и API.
`SameSite=None` в production не поддерживается: validation отклоняет
`AUTH_REFRESH_COOKIE_SAMESITE=none`. `Origin` login/refresh/logout
проверяется по origin самого API и `CORS_ORIGINS`. Если TLS завершается на
reverse proxy, ASGI-схема запроса может быть `http`, а Origin браузера —
`https://...`; в этом случае публичный origin frontend/API обязан быть явно
добавлен в `CORS_ORIGINS` (либо на прокси настраивается доверие
forwarded-заголовкам и соответствующая настройка приложения). Приложение не
доверяет неверифицированным `X-Forwarded-*` при проверке Origin.

Если Mini App открыт кнопкой-клавиатурой, `initData` может быть пустым;
авторизоваться по пустым или неподписанным данным нельзя. Клиент показывает
экран «Откройте приложение через меню бота» и вызывает
`Telegram.WebApp.openTelegramLink('https://t.me/<bot>?startapp=auth')`, чтобы
открыть Main Mini App с валидным `initData`. Для клиентов без Main Mini App
используется inline-кнопка `web_app` от бота.

Переменные окружения: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_INIT_DATA_MAX_AGE_SECONDS` (3600),
`TELEGRAM_INIT_DATA_MAX_FUTURE_SKEW_SECONDS` (60), `AUTH_JWT_SECRET`,
`AUTH_JWT_ISSUER` (`milly`), `AUTH_JWT_LEEWAY_SECONDS` (30),
`AUTH_ACCESS_TTL_SECONDS` (900), `AUTH_REFRESH_TTL_SECONDS` (2592000),
`AUTH_REFRESH_REUSE_GRACE_SECONDS` (30), `AUTH_REFRESH_COOKIE_NAME`,
`AUTH_REFRESH_COOKIE_SECURE`, `AUTH_REFRESH_COOKIE_SAMESITE`.

В production `TELEGRAM_BOT_TOKEN` и `AUTH_JWT_SECRET` обязательны. Validation
отклоняет токен без `:`, секрет короче 32 символов или содержащий
`change-me`, `AUTH_REFRESH_COOKIE_SECURE=false` и
`AUTH_REFRESH_COOKIE_SAMESITE=none`. Реальные значения не коммитятся: их
задают через окружение или secret manager.

## Админка

SQLAdmin доступна по `/admin` и включается, когда одновременно заданы
непустой `ADMIN_PASSWORD` и `AUTH_JWT_SECRET`; иначе `/admin` отвечает
стандартным JSON `404`, а приложение работает как обычно.

- Вход: `ADMIN_USERNAME` (по умолчанию `admin`) и `ADMIN_PASSWORD`;
  пароль сравнивается за постоянное время.
- Сессия: HttpOnly-cookie `milly_admin_session` с `SameSite=Lax`,
  `Path=/admin`, TTL `ADMIN_SESSION_MAX_AGE_SECONDS` (12 часов) и
  `Secure=AUTH_REFRESH_COOKIE_SECURE`.
- Cookie подписывается `AUTH_JWT_SECRET`; смена `ADMIN_PASSWORD`
  инвалидирует старые сессии.
- Доступны CRUD, поиск, сортировка и базовые фильтры для `User`,
  `TelegramProfile` и `AuthSession`. `refresh_token_hash` не показывается
  в списке и деталях.
- В production `ADMIN_PASSWORD` при включённой админке должен быть не
  короче 16 символов и не содержать `change-me`.
- `/admin` — HTML-интерфейс. Ошибка логина и необработанные ошибки
  SQLAdmin отдаются HTML/plain-text, а не в JSON-конверте; публичный
  JSON API это не затрагивает.
- Rate limiting и остальные общие middleware применяются к `/admin` как к
  обычному маршруту.

## Миграции

Приложение ходит в PostgreSQL через PgBouncer (`DATABASE_URL`), а Alembic —
напрямую (`DIRECT_DATABASE_URL`, порт `5432`). API изолирован от `db`, поэтому
autogenerate и upgrade в dev выполняются one-off контейнером `migrate`,
который подключён к сети `data` и видит `db:5432`. Каталог `./alembic`
монтируется с хоста, чтобы созданная ревизия осталась в репозитории.
Флаг `--build` обязателен: без него используется образ с запечённым на момент
сборки каталогом `alembic/`, и новая ревизия не попадёт в контейнер.

```bash
# создать ревизию (dev, файл появится в ./alembic/versions)
docker compose run --rm --build \
  -v "$PWD/alembic:/app/alembic" migrate \
  alembic revision --autogenerate -m "add table"

# применить миграции в dev
docker compose run --rm --build migrate
```

В production-подобном режиме миграции выполняет сервис `migrate` при
`docker compose -f compose.yml up`. После изменения `alembic/` образ нужно
пересобрать, иначе применится старая запечённая ревизия. Ручной запуск:

```bash
docker compose -f compose.yml run --rm --build migrate
```

Offline-проверка без подключения к БД:

```bash
poetry run alembic upgrade head --sql
```

## Задачи

- `POST /api/v1/tasks/ping` ставит задачу `ping` и возвращает
  `202 {"task_id": "<id>"}`; worker пишет результат в Redis.
- `heartbeat` запускается scheduler'ом по cron `*/5 * * * *` и пишет
  `task.heartbeat` в лог.
- Команды worker и scheduler (используются в `compose.yml`):

```bash
taskiq worker backend.tasks.worker:broker backend.tasks.example \
  --workers 2 --max-async-tasks 20 --wait-tasks-timeout 300 \
  --no-configure-logging

taskiq scheduler backend.tasks.worker:scheduler backend.tasks.example \
  --skip-first-run --no-configure-logging
```

Доставка NATS JetStream — **at-least-once**: задача может выполниться
повторно, поэтому задачи обязаны быть идемпотентными. Для долгих задач нужно
задавать собственный `timeout` (декоратор `@broker.task(timeout=...)`) строго
меньше `NATS_ACK_WAIT` (по умолчанию 300 с); примеры `ping` и `heartbeat`
тривиальны и завершаются мгновенно, поэтому `timeout` у них не задан. Worker
ждёт завершения задач 300 с
(`--wait-tasks-timeout`), а `stop_grace_period` контейнера — 330 с. DLQ нет:
после `NATS_MAX_DELIVER` (5) сообщение остаётся в stream до истечения
`NATS_STREAM_MAX_AGE` (7 дней).

## Тесты и проверки

```bash
poetry install
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
```

Тесты unit-уровня и не требуют PostgreSQL, NATS или Redis. Дополнительно
можно проверить конфигурацию Compose:

```bash
docker compose config --quiet
```

## Переменные окружения

Полный список с dev-значениями по умолчанию — в `.env.example`. Основные
группы:

- приложение: `APP_NAME`, `APP_ENV`, `DEBUG`, `API_V1_STR`, `ENABLE_DOCS`,
  `LOG_LEVEL`, `LOG_JSON`, `CORS_ORIGINS`, `ALLOWED_HOSTS`;
- PostgreSQL: `POSTGRES_*`, `DATABASE_URL`, `DIRECT_DATABASE_URL`,
  `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`, `SQL_ECHO`;
- NATS/taskiq: `NATS_*`, `TASKIQ_RESULT_EX_TIME`, `TASKIQ_KEEP_RESULTS`;
- Redis: `REDIS_URL`, `REDIS_PASSWORD`, `REDIS_HOST_PORT` (host-порт dev
  Compose, по умолчанию `6380`);
- Telegram Mini App auth: `TELEGRAM_*`, `AUTH_*`;
- админка: `ADMIN_USERNAME`, `ADMIN_PASSWORD`,
  `ADMIN_SESSION_MAX_AGE_SECONDS`;
- `PROVIDER_BASE_URL`, `PROVIDER_API_KEY` — зарезервированы для будущих
  интеграций и текущим скелетом не используются.

Для Compose обязательны `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
`NATS_USER`, `NATS_PASSWORD`, `REDIS_PASSWORD`, `TELEGRAM_BOT_TOKEN` и
`AUTH_JWT_SECRET` (переменные с `${...:?}` в `compose.yml`). Compose
подставляет `POSTGRES_*` и `REDIS_PASSWORD` в
`DATABASE_URL`/`DIRECT_DATABASE_URL`/`REDIS_URL` как есть, поэтому эти
значения должны состоять из URL-safe символов. Percent-encoding для них не
подходит: он изменит фактический пароль/имя и сломает аутентификацию.

Если `DATABASE_URL`, `DIRECT_DATABASE_URL` или `REDIS_URL` задаются вручную
(например, для локального запуска через Poetry), специальные символы в
логине, пароле или имени БД нужно percent-encode.

## Ограничения скелета

- Нет email, frontend и бизнес-моделей; `models/` содержит только `User`,
  `TelegramProfile` и `AuthSession`.
- Scheduler не HA: допускается ровно одна реплика.
- PgBouncer работает в transaction mode; asyncpg использует отключённые
  prepared statement caches.
- Гарантия доставки — at-least-once; retry-политика и идемпотентность —
  ответственность задач. DLQ и алертов по `max_deliver` нет.
- PgBouncer, NATS и Redis используют плавающие теги; для production
  рекомендуется закрепить digest.
