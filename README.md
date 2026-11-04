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
│   │   └── routes/          # health, auth, palette, projects, labels,
│   │                        # tasks, task-series
│   ├── admin/               # SQLAdmin: вьюхи, вход, монтирование
│   ├── auth/                # initData, JWT, refresh-сессии и cookie
│   ├── core/                # настройки, логирование, middleware
│   │   └── errors.py        # коды ошибок и исключения
│   ├── db/                  # engine/session, declarative base, readiness
│   ├── models/              # ORM: auth и управление задачами
│   ├── task_management/     # сервисы, репозиторий и правила задач
│   └── tasks/               # broker, worker, scheduler, генерация и доставка
├── tests/                   # unit-тесты без инфраструктуры
├── compose.yml              # production-подобный стек
├── compose.override.yml     # dev-режим (подхватывается автоматически)
├── Dockerfile               # multi-stage: builder → dev / runtime
└── .env.example             # шаблон локального окружения
```

Правило зависимостей: `main.py` собирает приложение и подключает роутеры;
`api` зависит от `core`, `db`, `models`, `auth` и `task_management`; `auth` —
от `core` и `models`; `db` и `task_management` — от `core`, а
`task_management` также использует `models`; `tasks` — от `auth`, `core`,
`db` и `task_management`. `auth` не импортирует `api`. `api.errors` зависит
от `core.errors` и FastAPI; `core.errors` не импортирует `api`.

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

## Управление задачами

Все маршруты находятся под `API_V1_STR` (по умолчанию `/api/v1`) и требуют
access-токен в `Authorization: Bearer`. Ошибки используют общий конверт из
раздела «Ошибки API». Часовой пояс пользователя, по которому считаются
локальные дни и слоты `scheduled`-напоминаний, хранится в профиле:

```text
PATCH /api/v1/auth/me/timezone
{"timezone": "Europe/Moscow"}
200 {... "timezone": "Europe/Moscow" ...}
```

### Палитра

`GET /api/v1/color-palette` возвращает только активные цвета, упорядоченные по
`sort_order` и `id`. Проекты и метки хранят `color_id` и продолжают отдавать
выбранный цвет даже после деактивации записи палитры. Деактивировать или
удалить последний активный цвет нельзя (`409 conflict`); это правило
проверяется в SQLAdmin-вьюхе и API-сервисе, а не ограничением БД.

### Проекты

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/api/v1/projects?include_archived=false` | список проектов с прогрессом и цветом |
| `POST` | `/api/v1/projects` | создать проект (`201`) |
| `GET` | `/api/v1/projects/{project_id}` | один проект |
| `PATCH` | `/api/v1/projects/{project_id}` | частично обновить |
| `POST` | `/api/v1/projects/{project_id}/archive` | архивировать |
| `POST` | `/api/v1/projects/{project_id}/unarchive` | вернуть из архива |
| `DELETE` | `/api/v1/projects/{project_id}` | soft-delete (`204`) |

Архив скрывает проект из списков по умолчанию, останавливает генерацию его
серий и доставку напоминаний. Unarchive заново рассчитывает будущие слоты от
текущего времени без догоняющих отправок. Удаление помечает проект, его серии
и задачи удалёнными и отменяет активные доставки.

### Метки

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/api/v1/labels` | список меток с цветом |
| `POST` | `/api/v1/labels` | создать метку (`201`) |
| `PATCH` | `/api/v1/labels/{label_id}` | частично обновить |
| `DELETE` | `/api/v1/labels/{label_id}` | soft-delete, идемпотентно (`204`) |

### Задачи

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/api/v1/tasks` | список задач с фильтрами |
| `POST` | `/api/v1/tasks` | создать разовую задачу (`201`) |
| `GET` | `/api/v1/tasks/{task_id}` | одна задача |
| `PATCH` | `/api/v1/tasks/{task_id}` | частично обновить |
| `POST` | `/api/v1/tasks/{task_id}/complete` | завершить |
| `POST` | `/api/v1/tasks/{task_id}/uncomplete` | вернуть в работу |
| `DELETE` | `/api/v1/tasks/{task_id}` | удалить (`204`) |

Фильтры списка: `date` (локальный день пользователя), `project_ids`,
`priorities`, `status` (`all`, `open`, `completed`, `overdue`), `recurrence`
(`all`, `regular`, `one_off`), `q` и `include_archived_projects`. Повторяемые
параметры передаются несколько раз, например
`?project_ids=1&project_ids=2&priorities=urgent_important`.

Завершение экземпляра серии останавливает напоминания только этого экземпляра
и не завершает серию. `PATCH` экземпляра серии применяет изменения к серии и её
будущим незавершённым экземплярам; `DELETE` экземпляра удаляет серию по
правилам ниже.

### Повторяющиеся серии

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET` | `/api/v1/task-series` | список серий с метками и шаблонами напоминаний |
| `POST` | `/api/v1/task-series` | создать серию (`201`) |
| `GET` | `/api/v1/task-series/{series_id}` | одна серия |
| `PATCH` | `/api/v1/task-series/{series_id}` | частично обновить |
| `DELETE` | `/api/v1/task-series/{series_id}` | удалить (`204`) |

Поддерживается только ежедневная повторяемость: `frequency` всегда `daily`.
Серия задаёт локальное время `time_of_day`, IANA-часовой пояс `timezone`,
начальную дату `starts_on` и необязательную включающую конечную дату `ends_on`.

- При создании серии экземпляры генерируются начиная со следующего будущего
  наступления и до горизонта **90 дней**; прошедшие даты не бэкфиллятся.
- Если первое наступление находится за горизонтом, создаётся ровно один
  экземпляр на эту дату.
- `generated_through` содержит время последнего сгенерированного экземпляра;
  текущее время запуска записывается только пока маркер ещё не установлен
  (первичное создание или исчерпанный первый запуск). Повторная генерация не
  перематывает маркер назад и не выходит за 90-дневный горизонт.
- Периодическая задача `generate_task_series` идемпотентно догенерирует
  недостающие экземпляры в пределах горизонта.
- Изменение расписания пересоздаёт будущие незавершённые экземпляры; прошлые и
  завершённые экземпляры сохраняют снимок полей.
- Удаление серии скрывает её и будущие незавершённые экземпляры, но сохраняет
  историю прошедших и завершённых задач.

### Напоминания

Поддерживаются правила `before_due` (`offset_minutes`) и `scheduled`
(`interval_days` и `time_of_day`); на задачу или серию допускается не более
одного правила каждого вида. Экземпляры серии получают копии шаблонных правил.
`dispatch_task_reminders` доставляет наступившие напоминания идемпотентно по
ключу `(reminder_id, scheduled_for)`: повторный запуск не отправляет то же
напоминание дважды. Неудачная отправка повторяется с экспоненциальной паузой,
после пятой неудачи доставка помечается как `failed`.

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
- Доступны CRUD, поиск, сортировка и базовые фильтры для `ColorPalette`,
  `User`, `TelegramProfile` и `AuthSession`. `refresh_token_hash` не
  показывается в списке и деталях.
- Вьюха `ColorPalette` не даёт менять `slug`, оставить ноль активных цветов
  или удалить цвет, на который ссылаются проект или метка; последний случай
  перехватывает `IntegrityError` и показывается как конфликт. Эти правила
  живут в SQLAdmin и API-сервисе, а не в ограничениях БД (кроме `ON DELETE
  RESTRICT` для используемых цветов).
- В production `ADMIN_PASSWORD` при включённой админке должен быть не
  короче 16 символов и не содержать `change-me`.
- `/admin` — HTML-интерфейс. Ошибка логина и необработанные ошибки
  SQLAdmin отдаются HTML/plain-text, а не в JSON-конверте; публичный
  JSON API это не затрагивает.
- Rate limiting и остальные общие middleware применяются к `/admin` как к
  обычному маршруту.
- Docker Compose передаёт `ADMIN_*` из `.env`; пустой `ADMIN_PASSWORD`
  оставляет `/admin` выключенной.

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

Планировщик запускает две фоновые задачи:

- `generate_task_series` — каждые 15 минут (`*/15 * * * *`, schedule_id
  `task-series-generation`) идемпотентно догенерирует будущие экземпляры
  ежедневных серий в горизонт 90 дней; пишет в лог `task.series_generated` с
  количеством созданных задач.
- `dispatch_task_reminders` — каждую минуту (`* * * * *`, schedule_id
  `reminder-delivery`) отправляет наступившие напоминания в Telegram и
  обновляет состояние доставки; пишет в лог `task.reminders_dispatched` с
  количеством успешных отправок.

Команды worker и scheduler (используются в `compose.yml`):

```bash
taskiq worker backend.tasks.worker:broker backend.tasks.generation backend.tasks.delivery \
  --workers 2 --max-async-tasks 20 --wait-tasks-timeout 300 \
  --no-configure-logging

taskiq scheduler backend.tasks.worker:scheduler backend.tasks.generation backend.tasks.delivery \
  --skip-first-run --no-configure-logging
```

Доставка NATS JetStream — **at-least-once**: задача может выполниться
повторно, поэтому задачи обязаны быть идемпотентными. Для долгих задач нужно
задавать собственный `timeout` (декоратор `@broker.task(timeout=...)`) строго
меньше `NATS_ACK_WAIT` (по умолчанию 300 с); генерация серий и доставка
напоминаний идемпотентны, не задают собственный `timeout` и завершаются в
пределах окна worker'а. Worker ждёт завершения задач 300 с
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
  `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_RECYCLE`,
  `DB_STATEMENT_TIMEOUT_MS`, `DB_POOL_TIMEOUT_SECONDS`, `SQL_ECHO`.
  `DB_STATEMENT_TIMEOUT_MS` задаёт и клиентский `command_timeout` asyncpg, и
  серверный `statement_timeout`. В Compose `DATABASE_URL` идёт через
  PgBouncer в transaction mode, который игнорирует `statement_timeout` в
  startup parameters (см. `IGNORE_STARTUP_PARAMETERS` в `compose.yml`),
  поэтому там действующим ограничением остаётся клиентский `command_timeout`;
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

- Нет email, frontend и AI-интеграций; `models/` содержит auth-модели и модели
  управления задачами, а `PROVIDER_*` пока не используются.
- Scheduler не HA: допускается ровно одна реплика.
- PgBouncer работает в transaction mode; asyncpg использует отключённые
  prepared statement caches.
- Гарантия доставки — at-least-once; retry-политика и идемпотентность —
  ответственность задач. DLQ и алертов по `max_deliver` нет.
- PgBouncer, NATS и Redis используют плавающие теги; для production
  рекомендуется закрепить digest.
