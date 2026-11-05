# Задачи, проекты, метки и напоминания: схема БД

- Дата: 2026-11-04
- Статус: на согласовании
- Область: `backend/` (FastAPI, Python 3.14, SQLAlchemy 2, PostgreSQL)
- Источник продукта: Figma-файл `Milly`, изученный через Figma MCP
- Потребитель: Telegram Mini App Milly

> Детальный дизайн переключателя регулярности, периодических серий и
> DELETE-матрицы — в `2026-11-04-task-recurrence-toggle-design.md`.

## 1. Зачем это нужно

В Figma описаны основные пользовательские сценарии Milly:

- главная с задачами на сегодня и прогрессом;
- список проектов, архив проектов, удаление проекта;
- создание и просмотр задачи;
- изменение срока, приоритета, проекта, меток и напоминаний;
- ежедневные регулярные задачи;
- поиск и фильтры по дате, проекту, приоритету, статусу и регулярности;
- голосовой ввод, который создаёт обычную задачу;
- глобальная палитра цветов для проектов и меток.

Сейчас в backend есть только `users`, `telegram_profiles` и `auth_sessions`.
Нужна схема данных, которая поддержит задачи, проекты, метки, историю
повторений и отправку напоминаний в Telegram. Финансы и воркспейсы в этой
итерации не реализуются: в Figma для них есть только пункты нижней
навигации без экранов и требований.

## 2. Цели

1. Хранить проекты с описанием, иконкой, цветом, архивом и мягким удалением.
2. Хранить пользовательские метки с названием и цветом; одна задача может
   иметь несколько меток.
3. Хранить разовые задачи.
4. Хранить ежедневные и периодические (каждые `N >= 2` дней) регулярные
   задачи и историю их экземпляров.
5. Хранить два типа напоминаний: «до срока задачи» и «по расписанию».
6. Отправлять напоминания в Telegram идемпотентно, с журналом доставок.
7. Хранить IANA-часовой пояс пользователя и корректно считать локальное
   время, включая переходы на летнее время.
8. Реализовать мягкое удаление проектов, серий, задач и меток.
9. Поддержать глобальную палитру цветов, управляемую из SQLAdmin, с 15
   цветами по умолчанию.
10. Поддержать экраны Figma индексами и запросами без денормализации
    счётчиков прогресса.
11. Не ломать существующие auth-модели, ошибки API и текущие тесты.

## 3. Не-цели

- Не реализовывать финансы и воркспейсы; вкладка «Воркспейсы» считается
  будущей.
- Не делать совместные проекты, роли, приглашения и шаринг.
- Не делать недельную, месячную или годовую регулярность: в MVP
  поддерживаются разовые задачи, ежедневные серии и периодические серии с
  интервалом `N >= 2` дней.
- Не делать пользовательские палитры или произвольные цвета: палитра
  глобальная и управляется только из админки.
- Не добавлять чек-листы, комментарии и вложения.
- Не делать полноценный offline-first sync и разрешение конфликтов
  между устройствами.
- Не реализовывать физическое удаление и retention-политику; исключение —
  detach-удаление будущих незавершённых экземпляров серии через
  `hard_delete_tasks`.
- Не делать UI и frontend-роутинг.
- Не добавлять push-уведомления вне Telegram.
- Не делать экспорт данных отдельным контрактом.
- Не менять существующий конверт ошибок API.
- Не добавлять новые runtime-зависимости Python: расчёт времени делается
  стандартной библиотекой.
- Не поддерживать отдельные исключения/правки одного экземпляра активной
  серии: редактирование из карточки применяется к серии и будущим
  экземплярам. Задача со soft-deleted серией трактуется как разовая.
  Исключение — переключение регулярности в `one_off` у задачи активной серии
  (detach): текущая задача открепляется, а будущие незавершённые экземпляры
  серии физически удаляются.
- Не поддерживать больше одного напоминания каждого типа на задачу или
  серию: в MVP это ровно один `before_due` и ровно один `scheduled`.
- Не делать фильтр по меткам в UI первой итерации; модель данных и индексы
  для него готовятся заранее.

## 4. Критерии неудачи

- Повторная генерация создаёт дубликаты экземпляров регулярной задачи.
- Правка серии меняет прошлые или завершённые экземпляры.
- Напоминание отправляется дважды или теряется при повторной доставке.
- Время задачи сдвигается из-за UTC, DST или смены часового пояса.
- Задача, серия или метка привязывается к сущности другого пользователя.
- Удалённый проект оставляет свои задачи видимыми в активных списках.
- Архив или удаление не останавливает генерацию и напоминания.
- Запросы списков не фильтруют `deleted_at` и показывают удалённые данные.
- Проект или метка может быть создана с неактивным цветом палитры.
- Удаление используемого цвета ломает проект или метку.
- В палитре можно оставить ноль активных цветов.
- После миграции в палитре не 15 цветов по умолчанию.
- Схема требует ручных SQL-обновлений помимо Alembic-миграции.
- Новые тесты требуют запущенного PostgreSQL там, где можно обойтись unit-тестами.

## 5. Принятые решения

1. `users.timezone` хранит IANA-зону, например `Europe/Moscow`.
   Клиент присылает её при входе или обновлении профиля.
2. Все абсолютные моменты хранятся в `TIMESTAMPTZ` в UTC.
   Локальные даты и время считаются через `zoneinfo`.
3. Регулярная задача представлена двумя сущностями:
   - `task_series` — шаблон серии с ежедневным (`daily`) или периодическим
     (`periodic`, каждые `N >= 2` дней) правилом;
   - `tasks` — материализованные экземпляры со снимком полей.
4. Правки серии применяются только к будущим незавершённым экземплярам.
   Прошлые и завершённые экземпляры не меняются.
5. Мягкое удаление используется для `projects`, `labels`, `task_series` и
   `tasks`. Для каскадного удаления проекта вводится `deletion_batch_id`,
   чтобы восстановление было возможно в будущем.
6. Архив проекта — отдельное состояние `archived_at`, не удаление.
7. Напоминания хранятся правилами в `task_reminders`, а факты отправки —
   в `reminder_deliveries`. Уникальный ключ
   `(reminder_id, scheduled_for)` обеспечивает идемпотентность.
8. Прогресс проектов и счётчики задач не денормализуются: они считаются
   запросами по `tasks`.
9. Перечисления хранятся как `VARCHAR` с `CHECK`, а не как PostgreSQL
   ENUM: так проще добавлять значения без сложных миграций типа.
10. Первичные ключи — `BIGINT` с `Identity()`, как в существующих моделях.
11. Связи «задача — проект», «задача — серия», «задача — метка» и
    «серия — метка» проверяются составными внешними ключами с `user_id`,
    чтобы исключить межпользовательские связи.
12. Горизонт материализации будущих экземпляров — 90 дней; Taskiq-задача
    продлевает его.
13. Удаление задачи серии следует матрице: активная будущая задача
    останавливает серию и мягко удаляет будущие незавершённые экземпляры,
    а завершённая, просроченная или задача soft-deleted серии удаляется
    отдельно; прошлые экземпляры остаются историей.
14. Первое напоминание «по расписанию» отправляется в первый `time_of_day`
    начиная с локального срока задачи, затем каждые `interval_days`.
15. Архив проекта скрывает проект и его задачи из активных списков,
    приостанавливает генерацию и напоминания. Разархивация возобновляет
    только будущее, без догоняющих отправок.
16. `due_at` обязателен для задачи, как в форме создания Figma.
17. Приоритет по умолчанию — `neither` («не срочная и не важная»).
18. Иконка проекта хранится строковым ключом; цвет проекта и метки
    хранятся ссылкой на глобальную палитру `color_palette`.
19. Отдельных операций редактирования одного экземпляра активной серии нет.
    Если задача имеет `series_id` активной серии, редактирование из карточки
    трактуется как действие над серией и будущими экземплярами, а удаление —
    по матрице из раздела 9.5. Если серия soft-deleted, задача трактуется как
    разовая: редактирование и удаление не трогают серию и её историю.
    Исключение — переключение `recurrence = "one_off"` у задачи активной
    серии: оно открепляет текущую задачу и физически удаляет будущие
    незавершённые экземпляры серии, кроме неё.
20. На задачу или серию допускается не больше одного напоминания каждого
    типа. Это закреплено уникальными индексами `(task_id, kind)` и
    `(series_id, kind)`.
21. Регулярность в MVP — ежедневная (`daily`, шаг 1 день) или
    периодическая (`periodic`, шаг `interval_days >= 2`). Слоты идут по фазе
    `starts_on + k * interval_days` в `time_of_day`; если выбранные
    `starts_on` и `time_of_day` ещё впереди, первый экземпляр создаётся на
    них, иначе — на первый слот фазы не раньше `now()`. Прошлое не
    backfill-ится.
22. Палитра цветов глобальная, не привязана к пользователю и управляется
    из SQLAdmin. В миграции создаются 15 цветов по умолчанию. Клиент
    получает только активные цвета, отсортированные по `(sort_order, id)`.
23. Метки принадлежат пользователю, не шарятся между пользователями и
    могут быть назначены на задачу или серию. На экземпляры серии метки
    копируются как снимок; правка серии обновляет только будущие
    незавершённые экземпляры.
24. Удаление используемого цвета палитры запрещено (`ON DELETE RESTRICT`).
    Админ деактивирует цвет через `is_active = false`; существующие
    проекты и метки продолжают его отображать, новые — выбрать не могут.
25. Нельзя удалить или деактивировать последний активный цвет палитры:
    сервис возвращает `409 conflict`.
26. Если цвет не передан при создании проекта или метки, сервер выбирает
    первый активный цвет по `(sort_order, id)`. В Figma по умолчанию
    выбран «Виноградный», поэтому slug `grape` стоит первым в seed-палитре.
27. Прогресс проекта считается по всем неудалённым задачам проекта;
    прогресс «на сегодня» — по задачам, попадающим в локальные сутки
    пользователя.

## 6. Схема данных

### 6.1. `users` (расширение существующей таблицы)

```text
timezone VARCHAR(64) NOT NULL DEFAULT 'UTC'
```

Правила:

- значение валидируется через `zoneinfo.ZoneInfo`;
- существующие пользователи после миграции получают `UTC`;
- при смене зоны новые задачи используют новую зону, а уже созданные
  серии сохраняют свою `task_series.timezone` до редактирования.

### 6.2. `color_palette` (глобальная палитра)

```text
id                  BIGINT PK (identity)
slug                VARCHAR(32) NOT NULL UNIQUE
name                VARCHAR(64) NOT NULL
hex                 CHAR(7) NOT NULL
sort_order          SMALLINT NOT NULL
is_active           BOOLEAN NOT NULL DEFAULT true
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
```

Правила:

- таблица глобальная, без `user_id`; управляется из SQLAdmin;
- `slug` — стабильный машиночитаемый ключ, например `grape`, `blue`;
  после создания не меняется; допустимы строчные латинские буквы, цифры,
  `_` и `-`;
- `name` — отображаемое название, редактируется админом;
- `hex` — цвет в формате `#RRGGBB`, валидируется CHECK;
- `sort_order` задаёт порядок в UI; при равенстве сортировка по `id`;
- `is_active = false` скрывает цвет из клиентской палитры, но не ломает
  существующие ссылки;
- удаление цвета, на который ссылаются проект или метка, запрещено
  (`ON DELETE RESTRICT`);
- нельзя удалить или деактивировать последний активный цвет: это правило
  SQLAdmin-вьюхи и API-сервиса, а не БД;
- клиент получает активные цвета через `GET /api/v1/color-palette`.

Цвета по умолчанию (seed-миграция):

| `sort_order` | `slug` | `name` | `hex` |
| --- | --- | --- | --- |
| 1 | `grape` | Виноградный | `#BB6BD9` |
| 2 | `blue` | Синий | `#2F80ED` |
| 3 | `sky` | Голубой | `#56CCF2` |
| 4 | `teal` | Бирюзовый | `#2D9CDB` |
| 5 | `green` | Зелёный | `#27AE60` |
| 6 | `mint` | Мятный | `#6FCF97` |
| 7 | `lime` | Лаймовый | `#B4D455` |
| 8 | `yellow` | Жёлтый | `#F2C94C` |
| 9 | `orange` | Оранжевый | `#F2994A` |
| 10 | `red` | Красный | `#EB5757` |
| 11 | `coral` | Коралловый | `#F26B5B` |
| 12 | `pink` | Розовый | `#F178B6` |
| 13 | `purple` | Фиолетовый | `#9B51E0` |
| 14 | `indigo` | Индиго | `#5B6CFF` |
| 15 | `gray` | Серый | `#828282` |

### 6.3. `projects`

```text
id                  BIGINT PK (identity)
user_id             BIGINT NOT NULL FK users.id ON DELETE CASCADE
name                VARCHAR(120) NOT NULL
description         TEXT NULL
color_id            BIGINT NOT NULL FK color_palette.id ON DELETE RESTRICT
icon                VARCHAR(32) NULL
archived_at         TIMESTAMPTZ NULL
deleted_at          TIMESTAMPTZ NULL
deletion_batch_id   UUID NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE (id, user_id)
FOREIGN KEY (color_id) REFERENCES color_palette(id)
    ON DELETE RESTRICT
UNIQUE (user_id, lower(name))
    WHERE deleted_at IS NULL AND archived_at IS NULL
```

Правила:

- `description` показывается в карточке проекта; `NULL` — описания нет;
- `icon` — ключ иконки из UI-кита Figma, например `briefcase-business`,
  `leaf`, `home`, `graduation-cap`; `NULL` означает иконку по умолчанию;
- `color_id` обязателен; при создании выбирается только активный цвет;
- Уникальность имени действует только среди активных проектов. Архивный
  проект может иметь то же имя, что и активный.
- `archived_at` и `deleted_at` взаимоисключающими не являются, но сервис
  не архивирует уже удалённый проект.
- `deletion_batch_id` заполняется при удалении проекта и его задач.

### 6.4. `labels`

```text
id                  BIGINT PK (identity)
user_id             BIGINT NOT NULL FK users.id ON DELETE CASCADE
name                VARCHAR(60) NOT NULL
color_id            BIGINT NOT NULL FK color_palette.id ON DELETE RESTRICT
deleted_at          TIMESTAMPTZ NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE (id, user_id)
FOREIGN KEY (color_id) REFERENCES color_palette(id)
    ON DELETE RESTRICT
UNIQUE (user_id, lower(name))
    WHERE deleted_at IS NULL
```

Правила:

- метка принадлежит пользователю и не шарится;
- `color_id` обязателен; при создании выбирается только активный цвет;
- имя уникально среди неудалённых меток пользователя без учёта регистра;
- переименование и смена цвета метки отражаются на всех задачах, где она
  назначена, потому что задачи ссылаются на метку, а не хранят её копию;
- удаление мягкое: назначения остаются в join-таблицах, но API скрывает
  удалённые метки; при будущем restore назначения возвращаются.

### 6.5. `task_series`

```text
id                  BIGINT PK (identity)
user_id             BIGINT NOT NULL FK users.id ON DELETE CASCADE
project_id          BIGINT NULL
title               VARCHAR(120) NOT NULL
description         TEXT NULL
priority            VARCHAR(32) NOT NULL DEFAULT 'neither'

frequency           VARCHAR(16) NOT NULL DEFAULT 'daily'
interval_days       SMALLINT NOT NULL DEFAULT 1
time_of_day         TIME NOT NULL
timezone            VARCHAR(64) NOT NULL

starts_on           DATE NOT NULL
ends_on             DATE NULL
generated_through   TIMESTAMPTZ NULL
reminders_enabled   BOOLEAN NOT NULL DEFAULT false

deleted_at          TIMESTAMPTZ NULL
deletion_batch_id   UUID NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE (id, user_id)
CHECK (frequency IN ('daily', 'periodic'))
CHECK (interval_days >= 1)
CHECK (
    (frequency = 'daily' AND interval_days = 1)
    OR (frequency = 'periodic' AND interval_days >= 2)
)
CHECK (ends_on IS NULL OR ends_on >= starts_on)
FOREIGN KEY (project_id, user_id)
    REFERENCES projects(id, user_id) ON DELETE RESTRICT
```

Правила:

- `frequency` — `daily` (шаг 1 день) или `periodic` (шаг
  `interval_days >= 2`); CHECK фиксирует допустимые пары значений;
- `interval_days` — шаг серии в локальных днях; для `daily` всегда `1`;
- `time_of_day` — локальное время в `timezone` серии;
- `starts_on` и `ends_on` — локальные даты в `timezone` серии;
  `ends_on` включительный и не раньше `starts_on`;
- `generated_through` — `due_at` последнего сгенерированного экземпляра;
- `deleted_at` останавливает генерацию и отправку напоминаний серии;
- `reminders_enabled` — мастер-тумблер «Автонапоминания» для серии.

### 6.6. `tasks`

```text
id                  BIGINT PK (identity)
user_id             BIGINT NOT NULL FK users.id ON DELETE CASCADE
series_id           BIGINT NULL
project_id          BIGINT NULL
title               VARCHAR(120) NOT NULL
description         TEXT NULL
priority            VARCHAR(32) NOT NULL DEFAULT 'neither'
due_at              TIMESTAMPTZ NOT NULL
completed_at        TIMESTAMPTZ NULL
reminders_enabled   BOOLEAN NOT NULL DEFAULT false

deleted_at          TIMESTAMPTZ NULL
deletion_batch_id   UUID NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE (id, user_id)
UNIQUE (series_id, due_at)
    WHERE series_id IS NOT NULL AND deleted_at IS NULL

FOREIGN KEY (project_id, user_id)
    REFERENCES projects(id, user_id) ON DELETE RESTRICT
FOREIGN KEY (series_id, user_id)
    REFERENCES task_series(id, user_id) ON DELETE RESTRICT
```

Правила:

- `series_id IS NULL` — разовая задача;
- `series_id IS NOT NULL` — экземпляр ежедневной или периодической серии;
- `title`, `description`, `priority` и `project_id` — снимок полей на
  момент создания экземпляра;
- `completed_at` — единственный признак выполнения. Отдельный статус не
  вводится;
- просрочка вычисляется как `due_at < now() AND completed_at IS NULL`;
  отдельного поля `overdue` нет;
- `reminders_enabled` — мастер-тумблер «Автонапоминания» для конкретной
  задачи;
- частичный уникальный индекс `(series_id, due_at)` защищает от
  дублирования экземпляров при повторной генерации.

### 6.7. `task_labels`

```text
task_id             BIGINT NOT NULL
label_id            BIGINT NOT NULL
user_id             BIGINT NOT NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()

PRIMARY KEY (task_id, label_id)
FOREIGN KEY (task_id, user_id)
    REFERENCES tasks(id, user_id) ON DELETE CASCADE
FOREIGN KEY (label_id, user_id)
    REFERENCES labels(id, user_id) ON DELETE CASCADE
```

Правила:

- одна и та же метка не может быть назначена задаче дважды;
- задача и метка обязаны принадлежать одному пользователю;
- при мягком удалении задачи или метки строки остаются, но чтения
  фильтруют удалённые сущности;
- при физическом удалении задачи или метки строки удаляются каскадно.

### 6.8. `series_labels`

```text
series_id           BIGINT NOT NULL
label_id            BIGINT NOT NULL
user_id             BIGINT NOT NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()

PRIMARY KEY (series_id, label_id)
FOREIGN KEY (series_id, user_id)
    REFERENCES task_series(id, user_id) ON DELETE CASCADE
FOREIGN KEY (label_id, user_id)
    REFERENCES labels(id, user_id) ON DELETE CASCADE
```

Правила:

- `series_labels` — шаблон меток серии;
- при генерации экземпляра в `task_labels` копируется набор `label_id`,
  а не имя или цвет метки; переименование и смена цвета отражаются на
  всех назначениях;
- правка меток серии обновляет `series_labels` и метки будущих
  незавершённых экземпляров; прошлые и завершённые не меняются.

### 6.9. `task_reminders`

```text
id                  BIGINT PK (identity)
user_id             BIGINT NOT NULL FK users.id ON DELETE CASCADE
task_id             BIGINT NULL
series_id           BIGINT NULL
kind                VARCHAR(16) NOT NULL
enabled             BOOLEAN NOT NULL DEFAULT true
offset_minutes      INTEGER NULL
interval_days       SMALLINT NULL
time_of_day         TIME NULL
timezone            VARCHAR(64) NULL
next_run_at         TIMESTAMPTZ NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE (id, user_id)
CHECK ((task_id IS NOT NULL)::int + (series_id IS NOT NULL)::int = 1)
UNIQUE (task_id, kind) WHERE task_id IS NOT NULL
UNIQUE (series_id, kind) WHERE series_id IS NOT NULL
FOREIGN KEY (task_id, user_id)
    REFERENCES tasks(id, user_id) ON DELETE CASCADE
FOREIGN KEY (series_id, user_id)
    REFERENCES task_series(id, user_id) ON DELETE CASCADE
```

Правила:

- `kind` — `before_due` или `scheduled`;
- `task_id` — правило конкретного экземпляра или разовой задачи;
- `series_id` — шаблон серии; из него правила копируются в новые
  экземпляры. У шаблонных строк `next_run_at` всегда `NULL`;
- `before_due`: `offset_minutes >= 0`; у экземпляра
  `next_run_at = due_at - offset`, у шаблонной строки серии
  `next_run_at = NULL`; `interval_days` и `time_of_day` должны быть `NULL`;
- `scheduled`: `interval_days > 0`, `time_of_day` обязателен,
  `offset_minutes` должен быть `NULL`;
- `timezone` для `scheduled` фиксирует зону, в которой считается время;
  для `before_due` не используется;
- мастер-выключатель — `tasks.reminders_enabled` или
  `task_series.reminders_enabled`; `enabled` управляет конкретным типом
  напоминания.

### 6.10. `reminder_deliveries`

```text
id                  BIGINT PK (identity)
reminder_id         BIGINT NOT NULL
user_id             BIGINT NOT NULL FK users.id ON DELETE CASCADE
scheduled_for       TIMESTAMPTZ NOT NULL
status              VARCHAR(16) NOT NULL DEFAULT 'pending'
attempts            SMALLINT NOT NULL DEFAULT 0
locked_at           TIMESTAMPTZ NULL
next_attempt_at     TIMESTAMPTZ NULL
sent_at             TIMESTAMPTZ NULL
telegram_message_id BIGINT NULL
last_error          TEXT NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()

UNIQUE (reminder_id, scheduled_for)
CHECK (attempts >= 0)
FOREIGN KEY (reminder_id, user_id)
    REFERENCES task_reminders(id, user_id) ON DELETE CASCADE
```

Правила:

- `status` — `pending`, `sending`, `sent`, `failed` или `cancelled`;
- `scheduled_for` — запланированный слот отправки; уникальность вместе с
  `reminder_id` делает повторную доставку идемпотентной;
- `sending` и `locked_at` — lease воркера: строка захватывается на время
  отправки, чтобы два воркера не отправили одно сообщение;
- если воркер упал, другой воркер может забрать `sending`-доставку после
  истечения lease;
- `attempts` и `next_attempt_at` используются для backoff при ошибках
  Telegram;
- `last_error` содержит только безопасное краткое описание, без токенов и
  персональных данных;
- `telegram_message_id` сохраняется для диагностики.

### 6.11. Значения перечислений

`priority` (`VARCHAR(32)`, `CHECK`) в `tasks` и `task_series`:

| Значение | Подпись в UI |
| --- | --- |
| `urgent_important` | Срочные и важные |
| `important_not_urgent` | Важные, но не срочные |
| `urgent_not_important` | Срочные, но не важные |
| `neither` | Не срочные и не важные |

`task_series.frequency` (`VARCHAR(16)`, `CHECK`):

- `daily` — ежедневная серия, `interval_days = 1`;
- `periodic` — периодическая серия, `interval_days >= 2`.

API-значения `TaskRecurrence` не хранятся отдельной колонкой, а вычисляются
по серии, включая soft-deleted:

| Значение | Условие |
| --- | --- |
| `one_off` | `tasks.series_id IS NULL` |
| `regular` | серия с `frequency = 'daily'` |
| `periodic` | серия с `frequency = 'periodic'` |

`task_reminders.kind` (`VARCHAR(16)`, `CHECK`):

- `before_due`;
- `scheduled`.

`reminder_deliveries.status` (`VARCHAR(16)`, `CHECK`):

- `pending`;
- `sending`;
- `sent`;
- `failed`;
- `cancelled`.

Во всех таблицах с `priority` добавляется
`CHECK priority IN (...)`; значения хранятся строками, а не PostgreSQL
ENUM. Цвета не являются enum: они хранятся в `color_palette` и могут
пополняться из админки.

## 7. Связи

```text
users 1---* projects
users 1---* labels
users 1---* task_series
users 1---* tasks
color_palette 1---* projects
color_palette 1---* labels
projects 1---* task_series
projects 1---* tasks
task_series 1---* tasks
tasks *---* labels      через task_labels
task_series *---* labels через series_labels
tasks 1---* task_reminders
task_series 1---* task_reminders
task_reminders 1---* reminder_deliveries
```

Составные внешние ключи:

- `task_series(project_id, user_id) -> projects(id, user_id)`;
- `tasks(project_id, user_id) -> projects(id, user_id)`;
- `tasks(series_id, user_id) -> task_series(id, user_id)`;
- `task_labels(task_id, user_id) -> tasks(id, user_id)`;
- `task_labels(label_id, user_id) -> labels(id, user_id)`;
- `series_labels(series_id, user_id) -> task_series(id, user_id)`;
- `series_labels(label_id, user_id) -> labels(id, user_id)`;
- `task_reminders(task_id, user_id) -> tasks(id, user_id)`;
- `task_reminders(series_id, user_id) -> task_series(id, user_id)`;
- `reminder_deliveries(reminder_id, user_id) -> task_reminders(id, user_id)`.

Это не даёт привязать задачу, серию, метку или напоминание к сущности
другого пользователя. Для `task_reminders` и `reminder_deliveries`
`user_id` денормализован для выборок воркера; согласованность обеспечивает
сервис, потому что владелец напоминания — ровно один из
`task_id`/`series_id`.

## 8. Индексы

```text
color_palette:
  UNIQUE (slug)
  INDEX (is_active, sort_order, id)

projects:
  UNIQUE (id, user_id)
  UNIQUE (user_id, lower(name))
      WHERE deleted_at IS NULL AND archived_at IS NULL
  INDEX (user_id, archived_at) WHERE deleted_at IS NULL
  INDEX (color_id)

labels:
  UNIQUE (id, user_id)
  UNIQUE (user_id, lower(name)) WHERE deleted_at IS NULL
  INDEX (user_id) WHERE deleted_at IS NULL
  INDEX (color_id)

tasks:
  UNIQUE (id, user_id)
  UNIQUE (series_id, due_at)
      WHERE series_id IS NOT NULL AND deleted_at IS NULL
  INDEX (user_id, due_at, id) WHERE deleted_at IS NULL
  INDEX (user_id, completed_at) WHERE deleted_at IS NULL
  INDEX (user_id, project_id, due_at) WHERE deleted_at IS NULL
  INDEX (user_id, priority, due_at) WHERE deleted_at IS NULL
  INDEX (user_id, series_id) WHERE deleted_at IS NULL
  INDEX (project_id, completed_at) WHERE deleted_at IS NULL

task_series:
  UNIQUE (id, user_id)
  INDEX (user_id, generated_through) WHERE deleted_at IS NULL

task_labels:
  PRIMARY KEY (task_id, label_id)
  INDEX (label_id, task_id)
  INDEX (user_id, label_id)

series_labels:
  PRIMARY KEY (series_id, label_id)
  INDEX (label_id, series_id)
  INDEX (user_id, label_id)

task_reminders:
  UNIQUE (id, user_id)
  INDEX (next_run_at) WHERE enabled AND next_run_at IS NOT NULL

reminder_deliveries:
  UNIQUE (reminder_id, scheduled_for)
  INDEX (status, next_attempt_at)
  INDEX (user_id, scheduled_for)
```

Для поиска по `title` и `description` на MVP достаточно `ILIKE`. Если
поиск станет узким местом, отдельной миграцией добавляется расширение
`pg_trgm` и GIN-индексы; в этой спеке оно не обязательно.

## 9. Жизненные циклы

### 9.1. Создание разовой задачи

1. Валидировать `due_at`, проект, метки, часовой пояс и активность цвета
   проекта.
2. Вставить `tasks` с `series_id = NULL`.
3. Вставить `task_labels` для выбранных активных меток пользователя.
4. Создать `task_reminders` для включённых типов напоминаний.
5. Для `before_due` вычислить `next_run_at = due_at - offset_minutes`;
   если момент уже в прошлом, напоминание не создаётся.
6. Для `scheduled` вычислить первый `time_of_day` начиная с локального
   `due_at`; если он в прошлом, взять следующий подходящий слот от `now()`.

### 9.2. Создание регулярной серии

1. Вставить `task_series` с `frequency = 'daily' | 'periodic'`,
   соответствующим `interval_days`, снимком полей и `timezone`.
2. Вставить `series_labels` для выбранных меток.
3. Создать шаблонные `task_reminders` с `series_id` и
   `next_run_at = NULL`.
4. Сгенерировать первый экземпляр: если выбранные `starts_on` и
   `time_of_day` ещё впереди — на них, даже если `starts_on` дальше
   горизонта; если момент уже прошёл — на первый слот фазы начиная с
   `now()`.
5. Генерировать будущие экземпляры по фазе
   `starts_on + k * interval_days` до горизонта 90 дней или до `ends_on`.
6. Для каждого экземпляра скопировать снимок полей и метки из
   `series_labels`, вычислив `next_run_at` напоминаний.
7. Обновить `task_series.generated_through`.

### 9.3. Правка и переключение регулярной задачи

Правка из карточки задачи для экземпляра активной серии трактуется как
правка серии. Отдельных исключений для одного экземпляра нет, кроме
переключения регулярности в `one_off` (detach).

1. Обновить `task_series`, включая `frequency` и `interval_days`, и
   шаблонные `task_reminders`.
2. Обновить `series_labels`.
3. Выбрать будущие незавершённые экземпляры:
   `series_id = :id AND due_at > now() AND completed_at IS NULL
   AND deleted_at IS NULL`.
4. Обновить их снимки, метки и напоминания. Если изменилось расписание
   (`time_of_day`, `timezone`, `starts_on`, `ends_on`, `frequency` или
   `interval_days`), мягко удалить старые будущие экземпляры и
   сгенерировать новые по фазе `starts_on + k * interval_days`; старые
   остаются в БД как удалённые.
5. Прошлые и завершённые экземпляры не трогать.
6. Пересчитать `generated_through` при изменении расписания.

Переключение `recurrence` в `PATCH /api/v1/tasks/{id}`:

- `one_off -> regular/periodic`: сначала применяются остальные поля, затем
  создаётся серия из снимка задачи (`regular` — `frequency = 'daily'`,
  `interval_days = 1`; `periodic` — `frequency = 'periodic'`,
  `interval_days >= 2`) в авторской таймзоне. Текущая задача становится
  первым экземпляром новой серии.
- `series -> one_off`: текущая задача открепляется (detach), будущие
  незавершённые экземпляры серии, кроме неё, физически удаляются,
  шаблонные напоминания отменяются, серия soft-delete; у текущей задачи
  `series_id = NULL`. Завершённые и просроченные экземпляры остаются
  историей.
- При переключении `one_off <-> series`, если незавершённая текущая задача
  просрочена (`due_at <= now`), её `due_at` переносится на первый слот фазы
  строго после сегодняшнего локального дня с сохранением локального времени.
  Для включения якорь — локальная дата итогового `due_at`, для выключения —
  `series.starts_on`. Смена `regular <-> periodic` у активной серии
  пересоздаёт только будущие экземпляры и не двигает просроченную задачу.
- Детальный контракт и матрица валидации — в
  `2026-11-04-task-recurrence-toggle-design.md`.

### 9.4. Выполнение и возврат задачи

- Выполнение: `completed_at = now()`.
- Все ожидающие и неудавшиеся доставки этой задачи переводятся в
  `cancelled`; `task_reminders.next_run_at` обнуляется.
- Для регулярной задачи выполнение влияет только на текущий экземпляр;
  серия продолжается.
- Возврат: `completed_at = NULL`, напоминания пересоздаются от `now()`.
  Пропущенные в прошлом слоты не догоняются.

### 9.5. Мягкое удаление

- Разовая задача: `deleted_at = now()`, ожидающие доставки отменяются.
- Удаление задачи (`DELETE /api/v1/tasks/{id}`) следует матрице:

  | Состояние задачи | Поведение |
  | --- | --- |
  | Разовая задача или задача soft-deleted серии | soft-delete только задачи, доставки отменить |
  | Активная незавершённая задача серии (`completed_at IS NULL` и `due_at > now`) | soft-delete серии и всех будущих незавершённых экземпляров, включая более ранние, чем удаляемая |
  | Завершённая задача серии | soft-delete только задачи, серия и остальные экземпляры не трогаются |
  | Просроченная незавершённая задача серии (`due_at <= now`) | soft-delete только задачи, серия и остальные экземпляры не трогаются |

- Проект: генерируется `deletion_batch_id`, `deleted_at` ставится
  проекту, его сериям и задачам, доставки отменяются.
- Метка: `deleted_at = now()`; строки `task_labels` и `series_labels`
  остаются, но API исключает удалённую метку из ответов. Новые назначения
  удалённой метки запрещены.
- Все read-запросы фильтруют `deleted_at IS NULL`.
- Частичные уникальные индексы игнорируют удалённые строки.
- Восстановление по `deletion_batch_id` в эту итерацию не входит; поле
  добавляется как дешёвая страховка для будущего restore. Если restore
  появится, напоминания восстанавливаемых задач пересчитываются от
  `now()`, прошлые доставки не переотправляются.

### 9.6. Архив проекта

- `archived_at = now()`.
- Проект и его задачи исключаются из активных списков.
- Генерация повторений и отправка напоминаний для задач проекта
  приостанавливаются.
- Разархивация снимает `archived_at`; генерация продолжается с текущего
  момента, пропущенные экземпляры и напоминания не догоняются.
- Если при разархивации имя конфликтует с активным проектом, операция
  завершается `409 conflict`; автоматического переименования нет.

### 9.7. Генерация повторений

Taskiq-задача периодически выбирает активные серии, у которых
`generated_through` меньше горизонта, и создаёт недостающие экземпляры.

- Генерация идемпотентна за счёт уникального индекса
  `(series_id, due_at)`.
- `ends_on` ограничивает генерацию.
- Экземпляры создаются в локальное `time_of_day` серии по фазе
  `starts_on + k * interval_days`; для `daily` шаг равен 1 дню.
- При возобновлении от `generated_through` берётся первый слот фазы строго
  после локальной даты маркера, а не «следующий день».
- Если `starts_on` и `time_of_day` ещё впереди, первый экземпляр
  создаётся на них, даже если `starts_on` дальше горизонта. Если выбранный
  момент уже прошёл, прошлые экземпляры не создаются; первый экземпляр —
  первый слот фазы не раньше `now()`.
- После генерации `generated_through` обновляется до последнего
  созданного `due_at`.
- Если серия исчерпана по `ends_on` или не создала ни одного экземпляра,
  `generated_through` выставляется в `now()`, чтобы задача не выбиралась
  при каждом запуске.

### 9.8. Отправка напоминаний

Taskiq-воркер раз в минуту выбирает правила с
`next_run_at <= now()`, `enabled = true`, включённым мастер-тумблером и
активной задачей.

1. Захватить строку `task_reminders` через `FOR UPDATE SKIP LOCKED`.
2. Проверить, что задача не выполнена, не удалена, а проект не в архиве.
3. Вставить `reminder_deliveries` по уникальному ключу
   `(reminder_id, scheduled_for)`. Если доставка уже `sent`, только
   продвинуть `next_run_at`.
4. Захватить доставку: `status = 'sending'`, `locked_at = now()`,
   `attempts += 1`, закоммитить.
5. Отправить сообщение через Telegram Bot API.
6. При успехе: `status = 'sent'`, `sent_at`, `telegram_message_id`,
   `locked_at = NULL`; продвинуть `next_run_at` на следующий слот.
7. При ошибке: `status = 'failed'` или `pending` с `next_attempt_at` и
   backoff, `locked_at = NULL`. После исчерпания попыток — `failed`, затем
   продвинуть слот, чтобы напоминание не застряло навсегда.
8. Если воркер упал, `sending`-доставка с истёкшим `locked_at`
   возвращается в обработку другим воркером.
9. Для `before_due` после успешной или окончательно неуспешной отправки
   `next_run_at = NULL`.

### 9.9. Смена часового пояса

- Клиент присылает IANA-зону; сервер валидирует её и обновляет
  `users.timezone`.
- Новые задачи и серии используют новую зону.
- Существующие серии сохраняют `task_series.timezone`; чтобы перенести
  их в новую зону, пользователь редактирует серию.
- Прошлые `due_at` не пересчитываются.

### 9.10. Пересчёт напоминаний при изменениях

Единая политика для правки разовой задачи, правки серии, архивации,
удаления и возврата задачи:

- `sent`-доставки никогда не пересоздаются и не отправляются повторно.
- `pending`, `failed` и `sending` доставки, которые стали неактуальны,
  переводятся в `cancelled`; `locked_at` очищается.
- Если новый слот совпадает с уже существующей `cancelled`-доставкой того
  же `reminder_id`, строка реактивируется в `pending`, а не создаётся
  заново; это сохраняет уникальность
  `(reminder_id, scheduled_for)`.
- Если новый слот совпадает с `sent`-доставкой, отправка пропускается.
- При правке `due_at` или настроек напоминания пересчитывается
  `next_run_at`; старые слоты отменяются до отправки.
- При архивации `next_run_at` обнуляется, `pending`/`failed`/`sending`
  доставки отменяются. При разархивации будущие слоты пересчитываются от
  `now()`, пропущенные слоты не догоняются.
- При удалении задачи или серии `next_run_at` обнуляется, а
  `pending`/`failed`/`sending` доставки отменяются.

### 9.11. Определение прогресса

- **Прогресс проекта:** все неудалённые задачи проекта; `total` — их
  количество, `completed` — с непустым `completed_at`, `remaining` —
  разница. Завершённые, просроченные и будущие задачи входят в общий
  прогресс.
- **Прогресс «на сегодня»:** неудалённые задачи, у которых `due_at`
  попадает в локальные сутки пользователя; `completed` считается по
  `completed_at`.
- **«Задач в планах»:** неудалённые незавершённые задачи пользователя,
  у которых проект не удалён и не архивирован, включая задачи без
  проекта.
- **«Активных проектов»:** количество неудалённых проектов с
  `archived_at IS NULL`.
- **«Архив проектов»:** количество неудалённых проектов с
  `archived_at IS NOT NULL`.

### 9.12. Управление палитрой из админки

- Палитра хранится в `color_palette` и доступна в SQLAdmin отдельной
  вьюхой с CRUD, поиском и сортировкой.
- Админ может создать цвет, изменить `name`, `hex`, `sort_order` и
  `is_active`; `slug` после создания не меняется. Обе проверки
  (`slug` immutable и «нельзя оставить ноль активных цветов») реализуются
  в `on_model_change` SQLAdmin-вьюхи, потому что админка ходит в БД
  напрямую, а не через API-сервис.
- Удаление цвета, который используется проектом или меткой, запрещено
  внешним ключом. SQLAdmin-вьюха перехватывает `IntegrityError` и
  показывает понятную ошибку; вместо удаления админ выставляет
  `is_active = false`.
- Нельзя удалить или деактивировать последний активный цвет:
  SQLAdmin-вьюха отклоняет изменение, API-сервис возвращает
  `409 conflict`.
- Клиент запрашивает `GET /api/v1/color-palette`; в ответ попадают только
  активные цвета, отсортированные по `(sort_order, id)`.
- Изменения палитры не переписывают `color_id` у существующих проектов и
  меток; они продолжают отображать выбранный цвет, даже если он
  деактивирован.
- Новые проекты и метки могут выбирать только активные цвета.

### 9.13. Жизненный цикл меток

- Пользователь создаёт метку с названием и активным цветом палитры.
- Метка может быть назначена на задачу или серию; на задаче допускается
  несколько меток, дубликаты запрещены первичным ключом.
- Переименование и смена цвета метки отражаются на всех назначениях,
  потому что задачи ссылаются на метку.
- Удаление метки мягкое: она исчезает из выбора и из ответов задач, но
  строки назначений сохраняются для будущего restore.
- Серия хранит шаблон меток в `series_labels`; новые экземпляры получают
  копию в `task_labels`. Правка серии обновляет только будущие
  незавершённые экземпляры.

## 10. Часовые пояса и DST

- Все `due_at`, `completed_at`, `next_run_at`, `scheduled_for` и
  `generated_through` — UTC в `TIMESTAMPTZ`.
- `starts_on`, `ends_on` и `time_of_day` — локальные значения в
  `task_series.timezone`.
- При генерации сначала строится локальная дата и время, затем она
  переводится в UTC через `zoneinfo`.
- Несуществующее локальное время при весеннем переводе сдвигается вперёд
  до первого существующего момента.
- Неоднозначное локальное время при осеннем переводе трактуется как
  первое вхождение (`fold=0`).
- Для границ «сегодня» в запросах вычисляются UTC-границы локального дня
  пользователя; локальная дата в `tasks` не дублируется.
- Фаза периодической серии считается в локальных календарных днях:
  `starts_on + k * interval_days`. Переходы DST не сдвигают дату фазы, а
  меняют только UTC-представление локального времени.
- Ежедневная и периодическая серии не создают экземпляры задним числом:
  генерация начинается с первого слота фазы не раньше `now()`.

## 11. Влияние на API

Точный контракт API в эту спеку не входит, но схема требует:

- приёма и обновления `timezone` в login или отдельном профильном endpoint;
- `GET /api/v1/color-palette` — активные цвета для проектов и меток;
- списка проектов с прогрессом, `description`, `icon` и `color`;
- списка меток пользователя и назначения `label_ids` задаче;
- меток в ответе задачи и карточке списка;
- списка задач с фильтрами `date`, `project_ids`, `priorities`, `status`,
  `recurrence` (`all`, `regular`, `periodic`, `one_off`), `q`;
- создания, редактирования, выполнения и удаления задач;
- `recurrence` в ответе задачи и в `PATCH /api/v1/tasks/{id}` вместе с
  `interval_days`;
- `frequency` (`daily` или `periodic`) и `interval_days` в API серий;
- управления проектами, архивом и удалением;
- управления метками и их назначением;
- управления напоминаниями;
- SQLAdmin-вьюхи для `color_palette`.

Ответы не должны содержать soft-deleted сущности. Ошибки используют
существующий конверт `AppError`/`ErrorCode`; новые коды добавляются только
при необходимости и через `core/errors.py`.

## 12. Миграции

Миграция создаётся вручную: autogenerate не соберёт корректно частичные
индексы, `CHECK`-констрейнты, составные внешние ключи и seed-данные.

Порядок:

1. `color_palette`.
2. Seed 15 цветов по умолчанию.
3. `ALTER TABLE users ADD COLUMN timezone ...`.
4. `projects` (с `color_id`).
5. `labels` (с `color_id`).
6. `task_series` (с `interval_days`).
7. `tasks`.
8. `task_labels`.
9. `series_labels`.
10. `task_reminders`.
11. `reminder_deliveries`.
12. Частичные уникальные индексы и индексы списков.

Откат — в обратном порядке; `color_palette` удаляется последней.
Seed-данные входят в миграцию, чтобы после `alembic upgrade head` палитра
сразу содержала 15 цветов. Периодический интервал добавляется миграцией
`0005_periodic_task_series` (см.
`2026-11-04-task-recurrence-toggle-design.md`). Проверка без БД:

```bash
poetry run alembic upgrade head --sql
```

## 13. Тестирование

Unit-тесты без PostgreSQL:

- ежедневная и периодическая генерация с `starts_on` в прошлом и в будущем;
- сохранение фазы `starts_on + k * interval_days` при возобновлении от
  `generated_through`;
- первый экземпляр, если `time_of_day` сегодня уже прошёл;
- отсутствие backfill прошлых экземпляров;
- `ends_on` останавливает генерацию;
- DST: несуществующее и неоднозначное локальное время;
- перевод локального времени в UTC и обратно;
- первый слот `scheduled`-напоминания;
- расчёт `before_due`;
- идемпотентность ключа `(series_id, due_at)` на уровне доменной логики;
- seed-палитра: 15 записей, уникальные `slug`, корректные `hex`;
- нельзя выбрать неактивный цвет;
- нельзя деактивировать последний активный цвет;
- копирование меток серии в будущие экземпляры;
- фильтрация soft-deleted сущностей в query-хелперах.

Проверки проекта:

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
```

## 14. Порядок реализации

1. Чистые функции ежедневной и периодической генерации и часовых поясов с
   TDD.
2. ORM-модель `ColorPalette`, константа seed-палитры из 15 цветов и
   SQLAdmin-вьюха.
3. ORM-модели `Project`, `Label`, `TaskSeries`, `Task`, `TaskLabel`,
   `SeriesLabel`, `TaskReminder`, `ReminderDelivery` и ручная
   Alembic-миграция.
4. Сервисы проектов, меток, задач и серий; мягкое удаление и архив.
5. `task_reminders`, `reminder_deliveries`, Taskiq-задачи генерации и
   отправки.
6. API-схемы и роуты; при необходимости — дополнительные SQLAdmin-вьюхи.
7. Полный прогон тестов, линтеров, mypy и обновление graphify.

## 15. Зафиксированные допущения

Эти пункты уже учтены в спеке; если они неверны, их нужно поменять до
реализации:

1. Воркспейсы и финансы — будущие вкладки; в этой итерации `workspace_id`
   не добавляется.
2. Регулярность: разовая, ежедневная и периодическая (каждые `N >= 2`
   дней); недельная, месячная и годовая регулярность не поддерживаются.
3. Палитра цветов глобальная, управляется из SQLAdmin; 15 цветов по
   умолчанию; пользовательских палитр нет.
4. Цвет палитры нельзя удалить, если он используется; вместо удаления —
   деактивация. Последний активный цвет удалить или деактивировать нельзя.
5. Если цвет не передан, сервер выбирает первый активный цвет по
   `(sort_order, id)`; первым в seed идёт «Виноградный» (`grape`).
6. Метки принадлежат пользователю, не шарятся и могут быть назначены на
   задачу или серию; на экземпляры серии метки копируются.
7. Удаление задачи серии следует матрице из раздела 9.5: серию
   останавливает только активная будущая задача; остальные удаляются
   отдельно, прошлые экземпляры сохраняются как история.
8. Напоминание «по расписанию» начинается с локального срока задачи, а не
   с момента создания.
9. Архив проекта скрывает его задачи из активных списков и
   приостанавливает напоминания.
10. `due_at` обязателен; приоритет по умолчанию — `neither`.
11. Иконка проекта — строковый ключ; цвет проекта и метки — ссылка на
    `color_palette`.
12. Горизонт генерации — 90 дней; он выносится в настройку, если появится
    потребность.
13. Отдельных операций редактирования одного экземпляра серии нет; исключение
    — переключение регулярности в `one_off` (detach).
14. На задачу/серию — не больше одного напоминания каждого типа.
15. Первый экземпляр серии — выбранные `starts_on`/`time_of_day`, если они
    ещё впереди; иначе первый слот фазы с `now()`. Дальше — по фазе
    `starts_on + k * interval_days` (для `daily` шаг 1 день).
16. Прошлое не backfill-ится; генерация начинается с `now()`.
17. При разархивации конфликт имени возвращает `409 conflict`.
18. Прогресс проекта считается по всем неудалённым задачам проекта,
    прогресс «на сегодня» — по локальным суткам пользователя.
19. Фильтр по меткам в UI первой итерации отсутствует; индексы и связи для
    него уже заложены.
