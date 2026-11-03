# SQLAdmin для владельца Milly

- Дата: 2026-11-03
- Статус: согласовано к реализации
- Область: `backend/` (FastAPI, Python 3.14, SQLAlchemy 2, SQLAdmin)

## 1. Зачем это нужно

Владельцу сайта нужна простая админка для просмотра и редактирования данных
Telegram Mini App без ручных SQL-запросов. Данные уже лежат в PostgreSQL:
`users`, `telegram_profiles`, `auth_sessions`. Нужен один защищённый вход по
паролю и стандартные CRUD-экраны.

Админка не должна усложнять публичный API, ломать существующие middleware
или требовать отдельного сервиса. Она монтируется в то же FastAPI-приложение
и использует тот же пул подключений к PostgreSQL.

## 2. Цели

1. Подключить `sqladmin` как зависимость проекта.
2. Смонтировать админку на `/admin`.
3. Включать админку только при заданных `ADMIN_PASSWORD` и
   `AUTH_JWT_SECRET`.
4. Пускать владельца по логину/паролю; пароль задаётся в `.env`.
5. Дать CRUD для `User`, `TelegramProfile`, `AuthSession`.
6. Добавить базовый поиск, сортировку и фильтрацию по осмысленным полям.
7. Не показывать `auth_sessions.refresh_token_hash` в списке и деталях.
8. Не тащить в админку отдельный engine и отдельную схему БД.
9. Сохранить единый жизненный цикл приложения: один `Database`, один
   broker, корректное освобождение ресурсов.
10. Покрыть конфигурацию и поведение админки unit-тестами без PostgreSQL.

## 3. Не-цели

- Не делать роли, RBAC и несколько администраторов.
- Не делать OAuth/SSO для админки.
- Не добавлять аудит-таблицу действий администратора.
- Не делать кастомные dashboard-страницы, графики и отчёты.
- Не добавлять бизнес-модели, которых ещё нет в проекте.
- Не переводить интерфейс SQLAdmin на русский и не подключать i18n.
- Не делать CSRF-токены сверх возможностей SQLAdmin: защита сессии
  строится на подписанной HttpOnly cookie и `SameSite=Lax`.
- Не менять контракт ошибок публичного API.

## 4. Критерии неудачи

- Админка доступна без пароля или при пустом `ADMIN_PASSWORD`.
- Пароль или сессионный секрет попадают в логи, ответы или репозиторий.
- При смене пароля старая сессия продолжает работать.
- Админка создаёт второй engine или второй пул подключений.
- При выключенной админке приложение падает или меняет поведение
  существующих маршрутов.
- Формы админки ломаются на relationship-полях.
- `refresh_token_hash` виден в списке или деталях сессий.
- В production принимается слабый пароль или плейсхолдер `change-me`.
- Тесты админки требуют запущенного PostgreSQL.

## 5. Ограничения

- Python 3.14, FastAPI 0.156.x, SQLAlchemy 2.x, Pydantic 2.x.
- Новые runtime-зависимости — только `sqladmin` и его extra `auth`
  (`itsdangerous`).
- Секреты не коммитятся; `.env.example` содержит только dev-плейсхолдеры.
- Пароль сравнивается за постоянное время.
- Сессия админки — подписанная cookie, без серверного хранилища.
- Публичный JSON API и его ошибки не меняются.
- Все новые функции и классы имеют докстринги по шаблону проекта.

## 6. Решения

### 6.1. Зависимость

Устанавливается `sqladmin[auth]`, а не просто `sqladmin`: extra `auth`
добавляет `itsdangerous`, без которого `SessionMiddleware` и
`AuthenticationBackend` не работают.

### 6.2. Конфигурация

Добавляются настройки:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `ADMIN_USERNAME` | `admin` | Логин владельца, непустой |
| `ADMIN_PASSWORD` | пусто | Пароль; пустое значение выключает админку |
| `ADMIN_SESSION_MAX_AGE_SECONDS` | `43200` | TTL сессионной cookie, 12 часов |

Правила:

- `admin_enabled` истинно, только если `ADMIN_PASSWORD.strip()` непустой и
  `AUTH_JWT_SECRET` непустой.
- Если `ADMIN_PASSWORD` задан без `AUTH_JWT_SECRET`, админка не
  монтируется, а в лог пишется предупреждение.
- В production при заданном `ADMIN_PASSWORD` (даже если он состоит только
  из пробелов) `ADMIN_PASSWORD.strip()` должен быть не короче 16 символов,
  а исходный пароль не должен содержать `change-me` без учёта регистра;
  иначе `ValidationError`.
- `ADMIN_SESSION_MAX_AGE_SECONDS` должен быть положительным.

### 6.3. Аутентификация и сессия

Создаётся `AdminAuth(AuthenticationBackend)`:

- логин и пароль сравниваются с настройками через
  `secrets.compare_digest` по UTF-8 байтам;
- при успешном входе в сессию кладётся HMAC-SHA256-отпечаток
  `username:password`, вычисленный с `AUTH_JWT_SECRET`;
- `authenticate` сравнивает отпечаток из сессии с текущим, поэтому смена
  пароля инвалидирует старые cookie;
- `logout` очищает сессию;
- cookie: имя `milly_admin_session`, `Path=/admin`, `HttpOnly`,
  `SameSite=Lax`, `Max-Age=ADMIN_SESSION_MAX_AGE_SECONDS`,
  `Secure=AUTH_REFRESH_COOKIE_SECURE` (в production всегда `true`);
- подпись cookie — `AUTH_JWT_SECRET`; отдельный секрет не вводится.

SQLAdmin сам добавляет `SessionMiddleware` в sub-app через
`AuthenticationBackend`, поэтому менять глобальный порядок middleware не
нужно.

### 6.4. Модели и экраны

Все три модели получают стандартный CRUD (`can_create`, `can_edit`,
`can_delete`, `can_view_details` включены).

SQLAdmin по умолчанию исключает из форм и primary key, и foreign key
колонки. Поэтому для редактирования связей:

- у всех вьюх ставится `form_include_pk = True`;
- `id` явно исключается через `form_excluded_columns`, чтобы владелец не
  мог менять первичный ключ;
- relationship-поля исключаются, чтобы SQLAdmin не подгружал связанные
  объекты при построении формы; FK-колонка (`user_id`) остаётся в форме
  как обычное целочисленное поле.

Итоговые вьюхи:

- `UserAdmin`: список `id`, `created_at`, `updated_at`; поиск по `id`;
  сортировка по этим же полям; фильтры-операции по `id` и датам;
  `form_excluded_columns = [User.id, User.telegram_profile]`.
- `TelegramProfileAdmin`: список `id`, `telegram_id`, `username`,
  `first_name`, `last_name`, `is_bot`, `is_premium`, `language_code`;
  поиск по `telegram_id`, `username`, `first_name`, `last_name`;
  сортировка по `id`, `telegram_id`, `is_premium`, `language_code`;
  фильтры `BooleanFilter` для `is_bot`/`is_premium`,
  `AllUniqueStringValuesFilter` для `language_code`,
  `ForeignKeyFilter` по `user_id` и `OperationColumnFilter` по
  `telegram_id`;
  `form_excluded_columns = [TelegramProfile.id, TelegramProfile.user]`.
- `AuthSessionAdmin`: список `id`, `user_id`, `family_id`, `created_at`,
  `expires_at`, `revoked_at`; поиск по `user_id`, `family_id`; сортировка
  по `id` и датам; фильтры `ForeignKeyFilter` по `user_id`,
  `OperationColumnFilter` по `family_id` и датам; `refresh_token_hash`
  исключён из списка, поиска и деталей;
  `form_excluded_columns = [AuthSession.id]`, поэтому `user_id` доступен в
  форме создания и редактирования.

У всех вьюх сортировка по `id` по убыванию. Импорт не включается; экспорт
остаётся стандартным.

### 6.5. Жизненный цикл приложения

Сейчас `Database` создаётся в `lifespan`, но SQLAdmin нужен engine при
сборке приложения. Меняем порядок:

- `create_app` создаёт `Database(settings)`, кладёт его в `app.state.db`
  и передаёт `database.engine` в `setup_admin`;
- `lifespan` больше не создаёт `Database`, а берёт `app.state.db` и
  освобождает его при остановке;
- создание engine остаётся ленивым: подключение не открывается до первого
  запроса.

Это сохраняет один пул подключений и не дублирует настройки PgBouncer.

### 6.6. Монтаж и отключение

`setup_admin(app, database, settings) -> Admin | None`:

- если `settings.admin_enabled` ложно, пишет `admin.disabled` в structlog и
  возвращает `None`, ничего не монтируя;
- иначе создаёт `Admin` с `base_url="/admin"`, заголовком
  `"{APP_NAME} Admin"`, engine из `Database`, `auth` и `debug` из настроек;
- добавляет три `ModelView`;
- возвращённый объект кладётся в `app.state.admin` для диагностики и
  тестов.

При выключенной админке `/admin` попадает в общий 404 публичного API и
отдаётся в JSON-конверте ошибок.

### 6.7. Ошибки и защита

- `/admin` — HTML-интерфейс, а не публичный JSON API. Ошибка логина
  отдаётся HTML-страницей SQLAdmin со статусом `400`; это осознанное
  исключение из JSON-конверта, как CORS preflight.
- Необработанные ошибки внутри `/admin` рендерит вложенное Starlette-
  приложение SQLAdmin (HTML или plain text), а не общий
  `ErrorEnvelopeMiddleware`: внутренний `ServerErrorMiddleware` отправляет
  ответ раньше, чем внешний конверт может его заменить. Это ещё одно
  документированное исключение; публичный JSON API оно не затрагивает.
- `Admin` получает `debug=settings.debug`. В production `DEBUG=true`
  запрещён существующей валидацией, поэтому traceback-страницы возможны
  только в локальной разработке.
- Rate limiting, `SecurityHeaders`, `BodySizeLimit`, `GZip`, CORS и
  проверка Host применяются к `/admin` как к обычному маршруту.
- Пароль и HMAC-отпечаток не логируются.
- `refresh_token_hash` не выводится в списке, поиске и деталях.
- Формы админки защищены `SameSite=Lax` и подписанной HttpOnly cookie;
  отдельный CSRF-токен не добавляется.

## 7. Структура файлов

```text
src/backend/admin/
├── __init__.py          # экспорт setup_admin
├── auth.py              # AdminAuth
├── setup.py             # setup_admin: Admin + view registration
└── views.py             # UserAdmin, TelegramProfileAdmin, AuthSessionAdmin
tests/
├── test_admin.py        # монтаж, login/logout, cookie, lifecycle
├── test_admin_auth.py   # AdminAuth unit-тесты
├── test_admin_views.py  # состав вьюх, поиск/фильтры/формы
└── test_config.py       # новые настройки и production-валидация
```

Изменяются `src/backend/core/config.py`, `src/backend/main.py`,
`pyproject.toml`, `poetry.lock`, `.env.example`, `README.md`.

## 8. Сценарии

1. `ADMIN_PASSWORD` пуст: приложение стартует, `/admin` -> 404 в
   JSON-конверте, админка не создаётся.
2. `ADMIN_PASSWORD` задан, `AUTH_JWT_SECRET` задан: `/admin` -> редирект на
   `/admin/login`, неверный пароль -> `400`, верный -> сессия и dashboard.
3. Пароль изменён в `.env` и приложение перезапущено: старая сессия не
   проходит `authenticate`, владелец снова входит.
4. В production задан слабый пароль: приложение не стартует с
   `ValidationError`.
5. Владелец открывает список профилей: доступны поиск по имени/username/
   telegram_id, фильтры по флагам и языку.
6. Владелец открывает список сессий: хеш refresh-токена не виден, можно
   фильтровать по пользователю и датам.

## 9. Тестирование

Тесты unit-уровня, без PostgreSQL, NATS и Redis:

- конфиг: значения по умолчанию, `admin_enabled`, production-валидация
  (короткий пароль, `Change-Me` в любом регистре, пароль из пробелов),
  положительный TTL;
- `AdminAuth`: успешный/неуспешный логин, logout, отсутствие сессии,
  поддельный отпечаток, инвалидирование при смене пароля, non-ASCII
  пароль;
- вьюхи: привязка к моделям, identity, состав списков, поиска, фильтров,
  исключение `refresh_token_hash`, `user_id` в формах профиля и сессии,
  отсутствие `id` в формах, успешное построение форм без БД;
- приложение: админка выключена без пароля/секрета, включена с обоими,
  редирект неавторизованного, логин/логаут через HTTP, атрибуты cookie,
  `Secure` в production, смена пароля инвалидирует cookie, три вьюхи
  зарегистрированы.

## 10. Открытые вопросы

Нет. Решения по паролю, сессии, набору полей и жизненному циклу engine
зафиксированы выше.
