# Аутентификация Telegram Mini App

- Дата: 2026-11-02
- Статус: согласовано к реализации
- Область: `backend/` (FastAPI, Python 3.14)
- Потребитель контракта: Telegram Mini App

## 1. Зачем это нужно

Milly открывается как Telegram Mini App. Пользователь уже
аутентифицирован в Telegram, а backend должен доверять только данным,
подписанным Telegram. Нельзя брать `initDataUnsafe`, `username` или
клиентский `telegram_id` как доказательство личности.

Нужен единый pipeline:

1. клиент присылает сырой `Telegram.WebApp.initData`;
2. backend проверяет HMAC и `auth_date`;
3. по стабильному `user.id` создаётся или обновляется профиль;
4. клиент получает короткий access JWT и refresh-сессию;
5. защищённые endpoint'ы требуют access в заголовке;
6. refresh ротируется и отзывается.

## 2. Цели

1. Один login-endpoint, который принимает `initData` и выдаёт пару
   access/refresh.
2. Внутренний `User` отделён от `TelegramProfile` связью O2O: в будущем
   у пользователя могут появиться другие способы входа.
3. Access JWT живёт 15 минут и проверяется без обращения к БД.
4. Refresh-токен живёт 30 дней, хранится только в HttpOnly cookie, в БД
   лежит только его SHA-256.
5. Строгая ротация refresh с reuse detection и коротким grace-окном.
6. Logout отзывает refresh-семейство и очищает cookie.
7. Ошибки login/refresh/logout не раскрывают, что именно не так, и не
   логируют токены или `initData`.
8. Контракт работает при `same-site`/`same-origin` frontend и API.
9. Keyboard-button запуск с пустым `initData` не ломает приложение:
   клиент предлагает открыть Main Mini App.

## 3. Не-цели

- Не реализовывать frontend, экраны и роутинг Mini App.
- Не делать роли, permissions, admin-панель и RBAC.
- Не добавлять вход по email, паролю, phone или другим провайдерам.
- Не делать OAuth2/OIDC server, consent screen и внешние клиенты.
- Не реализовывать отправку сообщений ботом, webhook и `answerWebAppQuery`.
- Не делать мгновенный отзыв access-токена: после logout access живёт до
  `exp`, максимум 15 минут.
- Не добавлять Redis-denylist, rate limiting и аудит-таблицу в этой
  итерации; оставляем точки расширения.
- Не менять формат успешных ответов существующих endpoint'ов.

## 4. Критерии неудачи

- Backend доверяет `initDataUnsafe` или данным, пришедшим не из
  проверенного `initData`.
- HMAC считается с исключением `signature` или без `auth_date`.
- `auth_date` не проверяется или принимается бесконечно старый.
- `user.id` не используется как стабильный субъект; идентичность
  строится на `username`.
- Refresh-токен хранится в БД в открытом виде.
- Refresh можно использовать повторно без отзыва семейства.
- Logout не отзывает refresh.
- Cookie выставлена без `HttpOnly`, без `Secure` в production или с
  `SameSite=None` без отдельной CSRF-защиты.
- Access можно получить по чужому `initData` после истечения окна.
- В логи попадают raw `initData`, access или refresh.
- Login с валидным `initData` падает из-за гонки двух параллельных
  запросов и создаёт двух пользователей.
- Keyboard-button запуск пытается авторизоваться по пустому `initData`.

## 5. Принятые решения

1. Access — JWT, HS256, 15 минут, только в памяти клиента.
2. Refresh — opaque-токен, 30 дней, HttpOnly cookie; в БД только
   SHA-256-хэш.
3. Refresh строго ротируется; повтор старого токена отзывает всё
   семейство после grace-окна 30 секунд.
4. Внутренние ID — `BIGINT`; `telegram_id` — `BIGINT`, потому что
   Telegram ID занимает до 52 бит и не влезает в PostgreSQL `INTEGER`.
5. `family_id` — UUID: это групповой идентификатор, а не PK сущности.
6. `auth_date` по умолчанию 1 час; значение конфигурируемое.
7. Frontend и API находятся на одном сайте; cookie работает без
   `SameSite=None`.
8. Access после logout не отзывается мгновенно; это осознанное окно
   безопасности.
9. Keyboard button с пустым `initData` показывает fallback-кнопку,
   открывающую Main Mini App.
10. `SecureStorage` не используется в основном сценарии, потому что
    refresh вообще не доступен JS. Он остаётся запасным вариантом, если
    позже появится cross-site deployment.
11. Ошибка refresh не очищает HttpOnly cookie; следующий успешный login
    перезаписывает её. Logout очищает cookie.

## 6. Схема данных

```text
users
- id BIGINT PK (identity)
- created_at TIMESTAMPTZ NOT NULL DEFAULT now()
- updated_at TIMESTAMPTZ NOT NULL DEFAULT now()

telegram_profiles
- id BIGINT PK (identity)
- user_id BIGINT NOT NULL UNIQUE FK users.id ON DELETE CASCADE
- telegram_id BIGINT NOT NULL UNIQUE
- is_bot BOOLEAN NULL
- first_name VARCHAR(64) NOT NULL
- last_name VARCHAR(64) NULL
- username VARCHAR(32) NULL
- language_code VARCHAR(16) NULL
- is_premium BOOLEAN NOT NULL DEFAULT false
- added_to_attachment_menu BOOLEAN NULL
- allows_write_to_pm BOOLEAN NULL
- photo_url TEXT NULL

auth_sessions
- id BIGINT PK (identity)
- user_id BIGINT NOT NULL FK users.id ON DELETE CASCADE
- family_id UUID NOT NULL
- refresh_token_hash CHAR(64) NOT NULL UNIQUE
- created_at TIMESTAMPTZ NOT NULL DEFAULT now()
- expires_at TIMESTAMPTZ NOT NULL
- revoked_at TIMESTAMPTZ NULL

INDEX auth_sessions(user_id)
INDEX auth_sessions(family_id)
INDEX auth_sessions(expires_at)
```

Правила:

- `telegram_profiles.user_id` — `UNIQUE`, иначе связь O2O превращается в
  O2M.
- `telegram_profiles.telegram_id` — `UNIQUE`, по нему ищется профиль.
- `User` не хранит `telegram_id`; Telegram-специфичные поля остаются в
  `TelegramProfile`.
- `photo_url` — `TEXT`, потому что URL может быть длинным.
- `is_bot` nullable: в `user` из `initData` поле обычно отсутствует; если
  оно пришло и равно `true`, login отклоняется.
- `is_premium` по умолчанию `false`.
- `auth_sessions` не входит в пользовательскую схему, но обязательна для
  ротации, reuse detection и logout.
- Просроченные сессии не удаляются автоматически в этой итерации;
  cleanup-задача — отдельное расширение.

## 7. Валидация `initData`

Вход — сырая query-строка `Telegram.WebApp.initData`.

Алгоритм:

1. Отклонить пустую строку.
2. Разобрать через `urllib.parse.parse_qsl` с `keep_blank_values=True`.
3. Отклонить duplicate keys.
4. Требовать `hash`, `auth_date`, `user`.
5. Собрать data-check-string: все полученные поля кроме `hash`,
   отсортированные по имени, `key=value`, соединённые `\n`, без
   завершающего `\n`.
6. `signature` **не исключать** из data-check-string.
7. `secret = HMAC-SHA256(key="WebAppData", message=bot_token)`.
8. `expected = HMAC-SHA256(key=secret, message=data_check_string)`.
9. Сравнить `expected` с полученным `hash` через
   `hmac.compare_digest`; сравнение только после проверки формата
   `[0-9a-f]{64}`.
10. Проверить `auth_date`:
    - не старше `TELEGRAM_INIT_DATA_MAX_AGE_SECONDS` (по умолчанию 3600);
    - не в будущем больше `TELEGRAM_INIT_DATA_MAX_FUTURE_SKEW_SECONDS`
      (по умолчанию 60).
11. Разобрать `user` как JSON и провалидировать:
    - `id` — целое > 0;
    - `first_name` — непустая строка;
    - `is_bot` не равен `true`;
    - остальные поля опциональны.
12. Вернуть доменный DTO `TelegramUser`.

Правила:

- `query_id` не обязателен и не используется для аутентификации.
- `auth_date` — Unix timestamp в секундах.
- Значения из query-парсера используются как есть; повторное
  URL-кодирование не выполняется.
- Ошибки валидации не раскрывают, какой именно шаг упал.
- `initData` не логируется.
- Окно в 1 час — компромисс между UX и replay-риском. Если login
  происходит сразу при запуске, безопаснее 10–15 минут; значение
  остаётся конфигом.

## 8. Login

`POST /api/v1/auth/telegram`

Запрос:

```json
{
  "init_data": "query_id=...&user=...&auth_date=...&hash=..."
}
```

Алгоритм:

1. `validate_init_data`.
2. Проверить, что `TELEGRAM_BOT_TOKEN` и `AUTH_JWT_SECRET` настроены;
   иначе `503 auth_not_configured`.
3. Взять `pg_advisory_xact_lock` по `telegram_id`, чтобы два
   параллельных login не создали дубль.
4. Найти `TelegramProfile` по `telegram_id` с загруженным `User`.
5. Если профиль есть:
   - обновить `first_name`, `last_name`, `username`, `language_code`,
     `is_premium`, `added_to_attachment_menu`, `allows_write_to_pm`,
     `photo_url`;
   - использовать существующий `User`.
6. Если профиля нет:
   - создать `User`;
   - создать `TelegramProfile` с O2O-ссылкой.
7. Создать `AuthSession`:
   - новый `family_id = uuid4()`;
   - `refresh_token = secrets.token_urlsafe(32)`;
   - `refresh_token_hash = sha256(refresh_token)`;
   - `expires_at = now + AUTH_REFRESH_TTL_SECONDS`.
8. Выпустить access JWT.
9. Выставить refresh cookie.
10. Вернуть `200` с access и профилем.

Одинаковый `200` для нового и существующего пользователя: клиенту не
нужно различать эти случаи.

Профиль обновляется при каждом login, потому что `username`, имена,
`photo_url` и `is_premium` могут меняться.

Транзакция и advisory lock удерживаются до commit, который выполняет
`Database.session()` в dependency cleanup.

Исключение — отзыв на ошибочном refresh: если отозвать сессию/семейство и
сразу выбросить `401`, `Database.session()` откатит транзакцию и отзыв
потеряется. Поэтому путь reuse/expiry использует
`revoke_session_and_commit` / `revoke_family_and_commit`, которые
фиксируют отзыв до выброса ошибки. Это единственное место, где auth
коммитит внутри сервиса; обычный login/logout коммитится dependency
cleanup.

## 9. Access JWT

Параметры:

- алгоритм: HS256;
- TTL: `AUTH_ACCESS_TTL_SECONDS` (по умолчанию 900);
- issuer: `AUTH_JWT_ISSUER` (по умолчанию `milly`);
- секрет: `AUTH_JWT_SECRET`, минимум 32 символа в production;
- leeway: `AUTH_JWT_LEEWAY_SECONDS` (по умолчанию 30).

Claims:

```json
{
  "sub": "<user.id>",
  "sid": "<auth_sessions.id>",
  "telegram_id": 123456789,
  "jti": "<uuid4 hex>",
  "type": "access",
  "iat": 1762000000,
  "exp": 1762000900,
  "iss": "milly"
}
```

Правила:

- `sub` и `sid` — строки с целыми числами; при разборе приводим к `int`.
- `type` обязан быть `"access"`.
- `telegram_id` кладём в claims, чтобы стандартные endpoint'ы могли
  обойтись без запроса к БД.
- `username` и другие изменяемые/PII-поля в JWT не кладём.
- Access передаётся только в `Authorization: Bearer <token>`.
- Access не хранится в cookie.
- Access после logout не отзывается; максимум 15 минут.
- Ошибки проверки JWT → `401 unauthorized`.

## 10. Refresh-сессии и ротация

Refresh-токен — opaque, 32 случайных байта в base64url. В БД хранится
только SHA-256 hex. Сырой токен возвращается только в cookie.

`POST /api/v1/auth/refresh`:

1. Прочитать refresh из HttpOnly cookie; нет cookie → `401`.
2. `sha256(token)`.
3. `SELECT ... FOR UPDATE` по `refresh_token_hash`.
4. Сессия не найдена → `401`; cookie не очищается (см. раздел 12).
5. `revoked_at` уже стоит:
   - если `now - revoked_at <= AUTH_REFRESH_REUSE_GRACE_SECONDS` —
     `401` без отзыва семейства (защита от гонки двух вкладок/повторного
     запроса);
   - иначе — отозвать все активные сессии с этим `family_id` через
     `revoke_family_and_commit` и вернуть `401`.
6. `expires_at <= now` → отозвать сессию через
   `revoke_session_and_commit` и вернуть `401`.
7. Иначе:
   - отозвать текущую сессию (`revoked_at = now`);
   - создать новую сессию в том же `family_id` с новым refresh;
   - выпустить новый access;
   - выставить новый refresh cookie;
   - вернуть access.
8. Commit.

Клиент обязан делать single-flight refresh: один запрос на все
одновременные `401`, остальные ждут результат.

Grace-окно не отменяет reuse detection: после него повтор старого
токена отзывает семейство. Grace нужен только для ложных срабатываний
из-за гонки.

## 11. Logout

`POST /api/v1/auth/logout`:

- читает refresh из cookie;
- если токен есть — находит сессию и отзывает всё `family_id`;
- если cookie нет — no-op;
- очищает cookie;
- возвращает `204`.

Access после logout остаётся валидным до `exp`, но не более 15 минут.
Если позже потребуется мгновенный отзыв, добавим Redis-denylist по
`sid`/`jti` с TTL access-токена; текущий контракт это допускает.

## 12. Cookie

Основной режим — same-site frontend и API.

Параметры:

| Параметр | Значение |
|---|---|
| Имя | `AUTH_REFRESH_COOKIE_NAME` (по умолчанию `milly_refresh`) |
| `HttpOnly` | да |
| `Secure` | `AUTH_REFRESH_COOKIE_SECURE`; `true` в production, `false` локально |
| `SameSite` | `AUTH_REFRESH_COOKIE_SAMESITE` (по умолчанию `lax`) |
| `Path` | `"{API_V1_STR}/auth"` |
| `Max-Age` | `AUTH_REFRESH_TTL_SECONDS` |

Правила:

- Cookie не доступна JS.
- Cookie отправляется только на auth-роуты, не на все API.
- `Path` вычисляется из `settings.api_v1_str`, чтобы не разъезжаться при
  смене префикса.
- `Domain` не выставляется.
- Для login/refresh/logout проверяется `Origin`, если он присутствует:
  разрешён либо origin из `CORS_ORIGINS`, либо origin самого API.
- `SameSite=Lax` и проверка `Origin` — базовая CSRF-защита.
- `SameSite=None` не используется в текущем same-site deployment. Если
  появится cross-site, потребуется отдельная CSRF-схема или возврат к
  refresh в body + `SecureStorage`.
- При ошибке refresh cookie не очищается: клиент получает `401` и
  повторно логинится, а успешный login перезаписывает cookie. Logout
  очищает cookie штатно. Это сознательный KISS-компромисс: очистка
  HttpOnly-cookie из exception handler требует отдельного механизма и не
  даёт безопасности, потому что украденная копия cookie всё равно
  остаётся у атакующего.

## 13. API-контракт

### 13.1 `POST /api/v1/auth/telegram`

Request:

```json
{ "init_data": "<raw initData>" }
```

Response `200`:

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 900,
  "user": {
    "id": 1,
    "created_at": "2026-11-02T12:00:00Z",
    "telegram_profile": {
      "telegram_id": 123456789,
      "is_bot": null,
      "first_name": "Ivan",
      "last_name": null,
      "username": "ivan",
      "language_code": "ru",
      "is_premium": false,
      "added_to_attachment_menu": null,
      "allows_write_to_pm": null,
      "photo_url": null
    }
  }
}
```

Response headers:

```text
Set-Cookie: milly_refresh=<opaque>; HttpOnly; Secure; SameSite=Lax; Path=/api/v1/auth; Max-Age=2592000
```

Ошибки:

- `401 unauthorized` — пустой/битый/просроченный `initData`, неверный
  HMAC, `is_bot=true`, отсутствие `user`.
- `403 forbidden` — недопустимый `Origin`.
- `503 auth_not_configured` — не задан bot token или JWT secret.
- `422 validation_error` — пустой `init_data` в теле.

### 13.2 `POST /api/v1/auth/refresh`

Request: без тела; refresh берётся из cookie.

Response `200`: как access-часть login, без `user`.

Response headers: новый `Set-Cookie`.

Ошибки:

- `401 unauthorized` — cookie отсутствует, токен неизвестен, истёк,
  отозван или повторно использован.
- `403 forbidden` — недопустимый `Origin`.

### 13.3 `POST /api/v1/auth/logout`

Request: без тела; refresh берётся из cookie.

Response `204`, cookie очищена.

### 13.4 `GET /api/v1/auth/me`

Request: `Authorization: Bearer <access>`.

Response `200`: `UserResponse` с профилем из БД.

Ошибки:

- `401 unauthorized` — заголовок отсутствует, токен невалиден или истёк;
  ответ содержит `WWW-Authenticate: Bearer`.

### 13.5 Защищённые endpoint'ы

Стандартная зависимость `CurrentAccessDep`:

- читает `Authorization: Bearer`;
- проверяет подпись, `exp`, `iss`, `type`;
- возвращает `AccessTokenClaims` без запроса к БД;
- при ошибке поднимает `UnauthorizedError` с
  `WWW-Authenticate: Bearer`.

`CurrentUserDep` — дополнительная зависимость для endpoint'ов, которым
нужен профиль: загружает `User` с `TelegramProfile` по `sub`.

## 14. Конфигурация

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | нет | SecretStr; обязателен в production |
| `TELEGRAM_INIT_DATA_MAX_AGE_SECONDS` | `3600` | Максимальный возраст `auth_date` |
| `TELEGRAM_INIT_DATA_MAX_FUTURE_SKEW_SECONDS` | `60` | Допустимое будущее `auth_date` |
| `AUTH_JWT_SECRET` | нет | SecretStr, >= 32 символов; обязателен в production |
| `AUTH_JWT_ISSUER` | `milly` | Claim `iss` |
| `AUTH_JWT_LEEWAY_SECONDS` | `30` | Leeway проверки JWT |
| `AUTH_ACCESS_TTL_SECONDS` | `900` | Время жизни access |
| `AUTH_REFRESH_TTL_SECONDS` | `2592000` | Время жизни refresh |
| `AUTH_REFRESH_REUSE_GRACE_SECONDS` | `30` | Grace-окно при reuse |
| `AUTH_REFRESH_COOKIE_NAME` | `milly_refresh` | Имя cookie |
| `AUTH_REFRESH_COOKIE_SECURE` | `true` | Флаг `Secure` |
| `AUTH_REFRESH_COOKIE_SAMESITE` | `lax` | `lax`/`strict`/`none` |

Валидация:

- В production `TELEGRAM_BOT_TOKEN` обязателен, непустой и содержит `:`
  (формат реального bot token — `<bot_id>:<secret>`).
- В production `AUTH_JWT_SECRET` обязателен, содержит минимум 32
  символа и не содержит `change-me` (защита от dev-плейсхолдера).
- В production `AUTH_REFRESH_COOKIE_SECURE` должен быть `true`, а
  `AUTH_REFRESH_COOKIE_SAMESITE` не может быть `none` (same-site
  deployment).
- TTL и grace — положительные целые.
- `AUTH_REFRESH_COOKIE_SAMESITE` — литерал из трёх значений.

## 15. Ошибки

Используем текущий `AppError`, расширив его необязательными
`headers: Mapping[str, str] | None`.

- `UnauthorizedError` — 401, код `unauthorized`.
- `ForbiddenError` — 403, код `forbidden`.
- `AuthNotConfiguredError` — 503, код `auth_not_configured`.

Правила:

- Для access `WWW-Authenticate: Bearer`.
- Для refresh/login заголовок не обязателен.
- Сообщения не раскрывают, какой именно шаг проверки упал.
- В логи пишем `user_id`, `session_id`, `event`, но не токены и не
  `initData`.
- `403 forbidden` используется только для проверки `Origin`.

Зависимость от плана стандартизации ошибок:

- Сейчас в проекте действует `AppError(status_code, code, message,
  details)`.
- Если план `2026-11-02-api-error-standardization` будет реализован
  раньше, `UnauthorizedError`, `ForbiddenError` и
  `AuthNotConfiguredError` переносятся в новый реестр без изменения
  публичных кодов и статусов.
- Auth-план не реализует стандартизацию ошибок и не переписывает
  `core/errors.py` целиком; он добавляет `headers`, `UnauthorizedError`,
  `ForbiddenError`, `AuthNotConfiguredError` и
  `RefreshTokenGraceError`.

## 16. Клиент

Основной сценарий (same-site cookie):

1. Загрузить `telegram-web-app.js` до кода приложения.
2. Вызвать `Telegram.WebApp.ready()`.
3. Если `initData` не пустой:
   - отправить его в `/auth/telegram`;
   - access хранить только в памяти;
   - refresh автоматически окажется в HttpOnly cookie.
4. При старте, если access нет, вызвать `/auth/refresh`.
5. При `401` от API — один refresh, остальные запросы ждут.
6. При неудачном refresh — повторный login по `initData`.
7. Если `initData` уже старше окна — попросить открыть Mini App заново.

Keyboard button:

- `initData === ''` — авторизация по `initData` невозможна by design.
- Показать экран «Откройте приложение через меню бота».
- Кнопка вызывает
  `Telegram.WebApp.openTelegramLink('https://t.me/<bot>?startapp=auth')`.
- Откроется Main Mini App, где `initData` уже есть.
- Для клиентов без Main Mini App (Bot API < 7.8) — fallback через
  inline-кнопку `web_app` от бота.

Запрещено:

- использовать `initDataUnsafe` для авторизации;
- хранить access в `localStorage`/`CloudStorage`;
- хранить refresh в JS-доступном хранилище в текущем cookie-режиме;
- отправлять access в query string.

## 17. Безопасность

- Bot token и JWT secret не попадают на клиент.
- HMAC считается по всем полям кроме `hash`; `signature` остаётся.
- Сравнение хэшей — constant-time.
- `auth_date` ограничен.
- `user.id` — единственный стабильный субъект.
- Refresh хранится как SHA-256; сырой токен — только в cookie.
- Cookie: `HttpOnly`, `Secure` в production, `SameSite=Lax`,
  ограниченный `Path`.
- `Origin` проверяется для cookie-роутов.
- Advisory lock защищает от гонки создания профиля.
- `SELECT FOR UPDATE` защищает от гонки refresh.
- Reuse detection отзывает семейство.
- В логах нет токенов, `initData`, `Authorization` и тел запросов.
- 500 не раскрывает внутренние детали (это обеспечивает текущий
  error-handler).
- Access после logout живёт до 15 минут; это документированное
  ограничение.

## 18. Наблюдаемость

События:

- `auth.login.succeeded` — `user_id`, `is_new_user`, `telegram_id`;
- `auth.login.failed` — причина на уровне `reason` без raw-данных;
- `auth.refresh.succeeded` — `user_id`, `session_id`;
- `auth.refresh.reused` — `user_id`, `family_id`;
- `auth.logout.succeeded` — `user_id`, `family_id`.

Правила:

- не логировать `init_data`, `access_token`, `refresh_token`,
  `Authorization`, cookie;
- `request_id` уже пробрасывается middleware;
- ошибки 4xx логируются на уровне warning/info, 5xx — с traceback.

## 19. Тестирование (TDD)

### 19.1 `initData`

- Валидный fixture с `user`, `auth_date`, `query_id`, `signature`.
- Подмена поля после подписи → 401.
- `signature` входит в HMAC: data-check-string без `signature` не
  проходит.
- Пустой `initData`.
- Нет `hash`.
- Некорректный формат `hash`.
- Duplicate keys.
- Старый `auth_date`.
- Будущий `auth_date` за пределами skew.
- Нет `user`.
- Битый JSON в `user`.
- `user.id` не целое или <= 0.
- `is_bot=true`.
- `query_id` отсутствует — login всё равно валиден.

### 19.2 JWT и refresh

- Round-trip access claims.
- Неверный секрет.
- Истёкший `exp`.
- Неверный `iss`.
- `type != "access"`.
- Битый `sub`/`sid`.
- Refresh — 32 случайных байта в base64url, хэш — 64 hex.
- `create_access_token` без JWT secret → `auth_not_configured`.

### 19.3 Сервис

- Login создаёт `User` + `TelegramProfile`.
- Login существующего обновляет профиль.
- Login вызывает advisory lock по `telegram_id`.
- Login отклоняет `is_bot=true`.
- Login не трогает репозиторий при невалидном `initData`.
- Login создаёт сессию с новым `family_id`, хэшем и TTL.
- Refresh ротирует access и refresh, сохраняет `family_id`.
- Refresh неизвестного токена → 401.
- Refresh истёкшего токена → 401 и зафиксированный revoke
  (`revoke_session_and_commit`).
- Reuse внутри grace → 401 без revoke семейства.
- Reuse вне grace → 401 и зафиксированный revoke семейства
  (`revoke_family_and_commit`).
- Logout по refresh отзывает семейство.
- Logout по `session_id` отзывает семейство.
- Повторный logout идемпотентен.
- В репозиторий не попадает сырой refresh.

### 19.4 API

- Login возвращает access, пользователя и cookie с нужными флагами.
- Login с невалидным `initData` → 401.
- Login с недопустимым `Origin` → 403.
- Refresh без cookie → 401.
- Refresh с cookie → новая пара и новая cookie.
- Refresh с истёкшим/повторным токеном → 401; очищающий `Set-Cookie`
  не выставляется.
- Logout очищает cookie и отзывает refresh.
- `/me` без access → 401 с `WWW-Authenticate`.
- `/me` с access → пользователь.
- Cookie `Path` соответствует `API_V1_STR`.
- В ответах и логах нет `init_data` и токенов.

### 19.5 Проверки проекта

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy src tests
docker compose config --quiet
```

## 20. Совместимость и миграция

- Добавляется миграция `0002_telegram_auth`; существующие таблицы не
  меняются.
- `users`, `telegram_profiles`, `auth_sessions` создаются с нуля.
- В `compose.yml` добавляются `TELEGRAM_BOT_TOKEN` и `AUTH_JWT_SECRET`
  как обязательные переменные production-подобного стека.
- В `.env.example` добавляются dev-плейсхолдеры
  `TELEGRAM_BOT_TOKEN=dev-telegram-bot-token` и
  `AUTH_JWT_SECRET=dev-jwt-secret-change-me-32-chars`; production-валидация
  их отклоняет. `AUTH_REFRESH_COOKIE_SECURE` локально `false`, в
  production — `true`.
- README обновляется: flow, endpoint'ы, cookie, keyboard fallback,
  конфиг, ограничения.
- Если план стандартизации ошибок ещё не реализован, auth использует
  текущий `AppError`; после его реализации коды `unauthorized`,
  `forbidden`, `auth_not_configured` сохраняются.

## 21. Риски

| Риск | Митигация |
|---|---|
| `auth_date` 1 час увеличивает replay-окно | Конфиг; при login сразу после запуска можно снизить до 10–15 минут; HTTPS; опциональный Redis replay-cache в будущем. |
| Гонка двух login | `pg_advisory_xact_lock` по `telegram_id`. |
| Гонка двух refresh | `SELECT FOR UPDATE`, grace 30 секунд, single-flight на клиенте. |
| Reuse detection приводит к ложному logout | Grace-окно; клиент сериализует refresh. |
| Cookie не работает при cross-site | Зафиксирован same-site deployment; при изменении — body + SecureStorage или reverse-proxy. |
| Access нельзя отозвать мгновенно | Окно 15 минут; при требовании — Redis-denylist. |
| Утечка JWT secret | SecretStr, минимум 32 символа, обязателен в production, не логируется. |
| Утечка bot token | Только server-side; не логируется; production-валидация. |
| Часы сервера и Telegram расходятся | `max_future_skew` 60 секунд; `auth_date` в UTC. |
| Рост `auth_sessions` | Индекс `expires_at`; cleanup-задача в следующей итерации. |
| Конфликт с планом стандартизации ошибок | Auth добавляет только `headers` и четыре auth-класса; коды сохраняются при миграции. |

## 22. Порядок реализации

1. Конфиг, `UnauthorizedError`/`ForbiddenError`/`AuthNotConfiguredError`,
   `validate_init_data` + тесты.
2. JWT и refresh-утилиты + тесты.
3. ORM-модели и миграция `0002`.
4. Репозиторий и login-сервис + тесты.
5. Refresh-ротация, reuse detection, logout + тесты.
6. API-роуты, cookie, зависимости, схемы + тесты.
7. README, `.env.example`, `compose.yml`, полный прогон проверок.
