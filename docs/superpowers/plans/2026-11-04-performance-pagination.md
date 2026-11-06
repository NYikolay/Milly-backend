# Performance, Pagination and Input Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement cursor pagination, batch queries, bounded date handling, series/reminder performance fixes, DB timeouts, input limits, and the critical missing tests.

**Architecture:** Pagination is implemented at the API boundary (opaque cursor codec) and in domain services (page dataclasses), with repository queries filtering by the order key. N+1 queries are replaced by batch `IN` queries. Date/schedule helpers jump directly to the current window instead of iterating day by day. Series generation and reminder dispatch get bounded, batched work. All behavior is covered by failing-first tests.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL, Alembic, pytest, pytest-cov.

**Spec:** `docs/superpowers/specs/2026-11-04-performance-pagination-design.md`

## Global Constraints

- No new runtime dependencies. Only dev dependency `pytest-cov (>=7.0,<8.0)`.
- Keep the existing flat error envelope and `ErrorCode` usage.
- Do not change auth, CORS, dev compose secrets/ports, admin, rate limiting, or error-envelope behavior.
- Pagination is intentionally breaking: `{items, next_cursor}`, default `limit=50`, max `100`.
- Date bounds: `2025-01-01 .. 2100-12-31` inclusive (UTC for datetimes).
- `description` max 5000 chars; `label_ids` max 100 items.
- DB defaults: `DB_STATEMENT_TIMEOUT_MS=30000`, `DB_POOL_TIMEOUT_SECONDS=10`.
- Reminder dispatch: batch 100, soft deadline 240 s, sequential sends.
- Every new/changed function and class requires an English docstring in the project template.
- Run after every task: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`.
- After implementation: `graphify update .`.
- Do not stage unrelated pre-existing working-tree changes. Stage only the files listed in each task.

## Review Focus

- Cursor with changed filters, invalid encoding, or another user's cursor returns `400` and never a wrong page.
- Exact date boundaries (`2025-01-01`, `2100-12-31`, timezone offsets) are accepted/rejected correctly and do not overflow.
- Page boundaries with equal `due_at` / equal `name` are stable because `id` is the tiebreaker; no duplicates or gaps on a stable dataset.
- Ended, already-caught-up, and far-future series are not repeatedly claimed; no missed instances.
- Reminder backlog above 100 or slow Telegram stops before the NATS ack window and resumes next run.

---

### Task 1: Date bounds and arithmetic schedule jumps

**Files:**
- Create: `src/backend/task_management/date_bounds.py`
- Modify: `src/backend/api/task_schemas.py`
- Modify: `src/backend/task_management/tasks.py:795-812`
- Modify: `src/backend/task_management/series.py:935-953`
- Modify: `src/backend/task_management/schedule.py:60-202`
- Modify: `src/backend/task_management/reminders.py:245-287`
- Test: `tests/test_date_bounds.py` (new)
- Test: `tests/test_task_management_schedule.py`
- Test: `tests/test_task_management_reminders.py`
- Test: `tests/test_tasks_api.py`
- Test: `tests/test_task_series_api.py`
- Test: `tests/test_task_management_tasks.py`
- Test: `tests/test_task_management_series.py`

**Interfaces:**
- Produces:
  - `MIN_SUPPORTED_DATE: date`
  - `MAX_SUPPORTED_DATE: date`
  - `MIN_SUPPORTED_DATETIME: datetime`
  - `MAX_SUPPORTED_DATETIME: datetime`
  - `require_supported_date(value: date) -> date`
  - `require_supported_datetime(value: datetime) -> datetime`
  - `daily_due_times(...)` and `first_scheduled_time(...)` keep their existing signatures.
  - `next_reminder_slot(...)` keeps its existing signature.
- Consumes: existing `localize_wall_time`, `TaskService._validate_due_at`, `SeriesService._validate_range`.

- [ ] **Step 1: Write failing date-bound tests**

Create `tests/test_date_bounds.py`:

```python
def test_require_supported_date_accepts_bounds() -> None:
    assert require_supported_date(date(2025, 1, 1)) == date(2025, 1, 1)
    assert require_supported_date(date(2100, 12, 31)) == date(2100, 12, 31)

def test_require_supported_date_rejects_outside() -> None:
    with pytest.raises(ValueError):
        require_supported_date(date(2024, 12, 31))
    with pytest.raises(ValueError):
        require_supported_date(date(2101, 1, 1))

def test_require_supported_datetime_accepts_bounds() -> None:
    assert require_supported_datetime(MIN_SUPPORTED_DATETIME) == MIN_SUPPORTED_DATETIME
    assert require_supported_datetime(MAX_SUPPORTED_DATETIME) == MAX_SUPPORTED_DATETIME

def test_require_supported_datetime_rejects_outside() -> None:
    with pytest.raises(ValueError):
        require_supported_datetime(MIN_SUPPORTED_DATETIME - timedelta(seconds=1))
    with pytest.raises(ValueError):
        require_supported_datetime(MAX_SUPPORTED_DATETIME + timedelta(seconds=1))
```

Add API tests:

```python
async def test_task_create_rejects_due_at_before_minimum(task_client):
    response = await task_client.post("/api/v1/tasks", json={
        "title": "Old", "due_at": "2024-12-31T23:59:59Z",
    })
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"

async def test_series_create_rejects_starts_on_outside_bounds(series_client):
    response = await series_client.post("/api/v1/task-series", json={
        "title": "Old", "time_of_day": "09:00:00", "timezone": "UTC",
        "starts_on": "2024-12-31",
    })
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
```

Update the two existing date-filter tests in `tests/test_tasks_api.py` (`9999-12-31`, `0001-01-01`) to expect `422` and `validation_error` instead of `400`.

Add schedule tests:

```python
def test_daily_due_times_jumps_over_a_century() -> None:
    now = datetime(2099, 12, 1, tzinfo=UTC)
    result = daily_due_times(
        starts_on=date(2025, 1, 1), ends_on=None,
        time_of_day=time(9, 0), timezone=ZoneInfo("UTC"),
        now=now, generated_through=None, horizon_days=90,
    )
    assert result
    assert all(item > now for item in result)

def test_first_scheduled_time_jumps_over_many_intervals() -> None:
    result = first_scheduled_time(
        due_at=datetime(2025, 1, 1, tzinfo=UTC), time_of_day=time(9, 0),
        timezone=ZoneInfo("UTC"), interval_days=7,
        now=datetime(2100, 12, 1, tzinfo=UTC),
    )
    assert result > datetime(2100, 12, 1, tzinfo=UTC)

def test_next_reminder_slot_jumps_over_many_intervals() -> None:
    reminder = _scheduled_reminder(interval_days=1, time_of_day=time(9, 0))
    result = next_reminder_slot(
        reminder, current=datetime(2025, 1, 1, 9, tzinfo=UTC),
        now=datetime(2100, 12, 1, tzinfo=UTC),
    )
    assert result is not None and result > datetime(2100, 12, 1, tzinfo=UTC)
```

Add service-boundary tests for direct calls (`TaskService.create/update`, `SeriesService.create/update`) asserting `BadRequestError`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_date_bounds.py tests/test_task_management_schedule.py tests/test_tasks_api.py tests/test_task_series_api.py -q`
Expected: FAIL because `date_bounds` does not exist and the loops are not optimized.

- [ ] **Step 3: Implement `date_bounds.py`**

Use the exact constants and functions from the spec. Both functions raise `ValueError("Date is out of the supported range")` / `ValueError("Datetime is out of the supported range")` when outside the inclusive bounds. `require_supported_datetime` must reject naive values with `ValueError("Datetime must be timezone-aware")`.

- [ ] **Step 4: Wire schema validators**

In `api/task_schemas.py`:
- `TaskCreate._validate_due_at`: after `_require_aware(value)`, call `require_supported_datetime(value)`.
- `TaskUpdate._validate_due_at`: when non-`None`, do the same.
- Add `_validate_supported_date` to `SeriesCreate` for `starts_on` and `ends_on` (skip `None` for `ends_on`).
- Add the same validator to `SeriesUpdate`.
- Add a `date` validator to `TaskFiltersQuery`.
- Keep validator docstrings in the project template.

- [ ] **Step 5: Wire service validation**

In `TaskService._validate_due_at`, after the awareness check, call `require_supported_datetime` and translate `ValueError` to `BadRequestError(_INVALID_DUE_AT)`.
In `SeriesService._validate_range`, call `require_supported_date` for both values before the range check and translate `ValueError` to `BadRequestError(_INVALID_RANGE)`.

- [ ] **Step 6: Replace day-by-day loops**

`daily_due_times`:
- Keep `ends_on` early return and `generated_through` resume.
- Compute `local_now = now.astimezone(timezone)`.
- Set `candidate_date = max(candidate_date, local_now.date())`.
- Compute `candidate = localize_wall_time(candidate_date, time_of_day, timezone)`; if `candidate <= now`, advance `candidate_date` by one day and recompute.
- Then keep the existing horizon loop (≤90 iterations).

`first_scheduled_time`:
- Keep the existing anchor: `candidate_date = due_at.astimezone(timezone).date()`.
- If candidate is before `due_at`, advance one day.
- Jump: `steps = max(0, (local_now.date() - candidate_date).days // interval_days)`; advance `candidate_date += timedelta(days=steps * interval_days)`; recompute.
- Then run the existing `while candidate <= now` loop (at most 1–2 iterations). Do not anchor on today: a future `due_at` must remain the first candidate.

`next_reminder_slot`:
- Replace the linear `while slot <= now` loop with the same arithmetic jump before the final 1–2 iterations. Keep the `OverflowError -> BadRequestError` conversion.

- [ ] **Step 7: Run tests to verify they pass**

Run: `poetry run pytest tests/test_date_bounds.py tests/test_task_management_schedule.py tests/test_task_management_reminders.py tests/test_tasks_api.py tests/test_task_series_api.py tests/test_task_management_tasks.py tests/test_task_management_series.py -q`
Expected: PASS.

- [ ] **Step 8: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/date_bounds.py src/backend/api/task_schemas.py src/backend/task_management/tasks.py src/backend/task_management/series.py src/backend/task_management/schedule.py src/backend/task_management/reminders.py tests/test_date_bounds.py tests/test_task_management_schedule.py tests/test_task_management_reminders.py tests/test_tasks_api.py tests/test_task_series_api.py tests/test_task_management_tasks.py tests/test_task_management_series.py
git commit -m "feat: bound user dates and optimize schedule jumps"
```

---

### Task 2: Cursor codec and response fields

**Files:**
- Create: `src/backend/api/pagination.py`
- Modify: `src/backend/api/task_schemas.py:121-124,563-566`
- Test: `tests/test_pagination.py` (new)

**Interfaces:**
- Produces:
  - `DEFAULT_PAGE_SIZE = 50`
  - `MAX_PAGE_SIZE = 100`
  - `MAX_CURSOR_LENGTH = 512`
  - `filter_fingerprint(values: Mapping[str, object]) -> str`
  - `encode_cursor(*, user_id: int, filter_fingerprint: str, order_key: Sequence[object]) -> str`
  - `decode_cursor(cursor: str, *, user_id: int, filter_fingerprint: str) -> list[object]`
- `decode_cursor` raises `BadRequestError("Invalid pagination cursor")`.
- `TaskListResponse` and `ProjectListResponse` gain `next_cursor: str | None = None`.

- [ ] **Step 1: Write failing codec tests**

Create `tests/test_pagination.py`:

```python
def test_cursor_roundtrip() -> None:
    token = encode_cursor(user_id=7, filter_fingerprint="abc", order_key=["2026-11-04T09:00:00+00:00", 42])
    assert "=" not in token
    assert decode_cursor(token, user_id=7, filter_fingerprint="abc") == ["2026-11-04T09:00:00+00:00", 42]

def test_cursor_rejects_wrong_user() -> None:
    token = encode_cursor(user_id=7, filter_fingerprint="abc", order_key=[1])
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=8, filter_fingerprint="abc")

def test_cursor_rejects_changed_filters() -> None:
    token = encode_cursor(user_id=7, filter_fingerprint="abc", order_key=[1])
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=7, filter_fingerprint="def")

@pytest.mark.parametrize("token", ["", "not-base64!", "e30", "eyJ2IjoyfQ"])
def test_cursor_rejects_invalid_payloads(token: str) -> None:
    with pytest.raises(BadRequestError):
        decode_cursor(token, user_id=7, filter_fingerprint="abc")

def test_filter_fingerprint_is_dict_key_order_insensitive() -> None:
    assert filter_fingerprint({"a": 1, "b": 2}) == filter_fingerprint({"b": 2, "a": 1})
```

Add a response-model test asserting both list responses expose `next_cursor`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_pagination.py -q`
Expected: FAIL because `api.pagination` does not exist.

- [ ] **Step 3: Implement the codec**

Implementation:
- `filter_fingerprint`: `hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()`.
- `encode_cursor`: build `{"v": 1, "u": user_id, "f": filter_fingerprint, "k": list(order_key)}`, JSON-encode, `base64.urlsafe_b64encode(...).decode().rstrip("=")`.
- `decode_cursor`: reject `len(cursor) > MAX_CURSOR_LENGTH`; restore padding; decode; require dict, version `1`, matching `u` and `f`, and `k` as a list; return `data["k"]`.
- Wrap every decode failure in `BadRequestError("Invalid pagination cursor")` with `from None`.
- Add full docstrings.

- [ ] **Step 4: Add response fields**

In `api/task_schemas.py`, add `next_cursor: str | None = None` to `TaskListResponse` and `ProjectListResponse`. Update their class docstrings.

- [ ] **Step 5: Run tests to verify they pass**

Run: `poetry run pytest tests/test_pagination.py -q`
Expected: PASS.

- [ ] **Step 6: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/api/pagination.py src/backend/api/task_schemas.py tests/test_pagination.py
git commit -m "feat: add opaque cursor codec"
```

---

### Task 3: Task pagination and batch enrichment

**Files:**
- Modify: `src/backend/task_management/tasks.py:38-58,90-140`
- Modify: `src/backend/task_management/ports.py:247-430`
- Modify: `src/backend/task_management/repository.py:764-850,993-1052`
- Modify: `src/backend/api/routes/tasks.py:85-119`
- Modify: `tests/task_management_fakes.py`
- Modify: `tests/test_tasks_api.py`
- Modify: `tests/test_task_management_tasks.py`
- Modify: `tests/test_task_management_integration.py`
- Test: `tests/test_task_management_repository.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class TaskListCursor: due_at: datetime; task_id: int`
  - `@dataclass(frozen=True) class TaskPage: items: Sequence[TaskDetail]; next_cursor: TaskListCursor | None`
  - `TaskService.list(user_id: int, filters: TaskFilters, *, limit: int, after: TaskListCursor | None = None) -> TaskPage`
  - Repository:
    - `list_tasks(..., limit: int, after_due_at: datetime | None, after_task_id: int | None) -> Sequence[Task]`
    - `list_task_labels_for_tasks(user_id, task_ids) -> dict[int, Sequence[Label]]`
    - `list_task_reminders_for_tasks(user_id, task_ids) -> dict[int, Sequence[TaskReminder]]`
    - `get_projects_by_ids(user_id, project_ids) -> dict[int, Project]`
- Consumes: `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE`, `MAX_CURSOR_LENGTH`, `encode_cursor`, `decode_cursor`, `filter_fingerprint` from Task 2.

- [ ] **Step 1: Write failing task pagination tests**

API tests in `tests/test_tasks_api.py` (extend `FakeTaskService`):

```python
async def test_task_list_returns_default_page_size(task_client, fake_task_service):
    for index in range(51):
        fake_task_service.add_detail(_task_detail(task_id=index + 1, due_at=NOW + timedelta(minutes=index)))
    response = await task_client.get("/api/v1/tasks")
    body = response.json()
    assert response.status_code == 200
    assert len(body["items"]) == 50
    assert body["next_cursor"] is not None

async def test_task_list_second_page_has_no_duplicates(task_client, fake_task_service):
    for index in range(51):
        fake_task_service.add_detail(_task_detail(task_id=index + 1, due_at=NOW + timedelta(minutes=index)))
    first = (await task_client.get("/api/v1/tasks")).json()
    second = (await task_client.get("/api/v1/tasks", params={"cursor": first["next_cursor"]})).json()
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(range(1, 52))
    assert second["next_cursor"] is None

async def test_task_list_rejects_filter_mismatch_cursor(task_client, fake_task_service):
    fake_task_service.add_detail(_task_detail(task_id=1, due_at=NOW))
    fake_task_service.add_detail(_task_detail(task_id=2, due_at=NOW + timedelta(minutes=1)))
    first = (await task_client.get(
        "/api/v1/tasks", params={"status": "open", "limit": 1}
    )).json()
    assert first["next_cursor"] is not None
    response = await task_client.get(
        "/api/v1/tasks", params={"status": "completed", "limit": 1, "cursor": first["next_cursor"]}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"

async def test_task_list_rejects_invalid_cursor(task_client):
    response = await task_client.get("/api/v1/tasks", params={"cursor": "not-base64"})
    assert response.status_code == 400

async def test_task_list_equal_due_at_uses_id_tiebreaker(task_client, fake_task_service):
    due_at = NOW
    for task_id in (3, 1, 2):
        fake_task_service.add_detail(_task_detail(task_id=task_id, due_at=due_at))
    ids = [item["id"] for item in (await task_client.get("/api/v1/tasks")).json()["items"]]
    assert ids == [1, 2, 3]

async def test_task_list_rejects_overlong_cursor(task_client):
    response = await task_client.get("/api/v1/tasks", params={"cursor": "x" * 513})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"

def test_task_list_openapi_documents_individual_filters(app):
    parameters = app.openapi()["paths"]["/api/v1/tasks"]["get"]["parameters"]
    names = {parameter["name"] for parameter in parameters}
    assert {"date", "project_ids", "priorities", "status", "recurrence", "q", "include_archived_projects", "limit", "cursor"} <= names
```

Service tests in `tests/test_task_management_tasks.py`:
- `test_list_returns_page_and_cursor`
- `test_list_uses_batch_related_queries` — fake repository records `list_task_labels_for_tasks`, `list_task_reminders_for_tasks`, `get_projects_by_ids` calls; assert each called once for 3 tasks.
- Update existing `service.list(...)` assertions to `page.items`.

Repository tests in `tests/test_task_management_repository.py`:
- `test_list_tasks_applies_cursor_and_limit` — compiled SQL contains `Task.due_at >` / `Task.id >` and `LIMIT`.
- `test_list_tasks_batch_methods_filter_by_user_and_ids` — compiled SQL contains `user_id` and `IN`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_tasks_api.py tests/test_task_management_tasks.py tests/test_task_management_repository.py -q`
Expected: FAIL because the page dataclasses and batch methods do not exist.

- [ ] **Step 3: Add domain dataclasses and service method**

In `task_management/tasks.py`, add the frozen dataclasses and change `TaskService.list`:
- Call repository with `limit + 1`.
- Slice to `limit`.
- Build `next_cursor` from the last kept task only when an extra row was returned.
- Call a new private `_details(user_id, tasks)` that uses the three batch repository methods and assembles `TaskDetail` in memory.
- Keep `get` using `_detail`.

- [ ] **Step 4: Implement repository pagination and batch methods**

`list_tasks`:
- Add `limit: int`, `after_due_at`, `after_task_id` parameters.
- Apply the cursor predicate with `or_(Task.due_at > after_due_at, and_(Task.due_at == after_due_at, Task.id > after_task_id))`.
- Add `.order_by(Task.due_at, Task.id).limit(limit)`.

Batch methods:
- Labels: `select(Label, TaskLabel.task_id).join(...).where(TaskLabel.user_id == user_id, TaskLabel.task_id.in_(task_ids), Label.user_id == user_id, Label.deleted_at.is_(None)).order_by(TaskLabel.task_id, Label.id)`; build dict.
- Reminders: same pattern with `TaskReminder`.
- Projects: `select(Project).where(Project.user_id == user_id, Project.deleted_at.is_(None), Project.id.in_(project_ids))`; build dict.
- Return `{}` for an empty input list without a query.

- [ ] **Step 5: Wire the route**

In `api/routes/tasks.py`:
- Add `limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE`.
- Add `cursor: Annotated[str | None, Query()] = None` — do not add FastAPI `max_length`; `decode_cursor` owns the 512-char limit and must return `400 bad_request`.
- Keep the seven existing filters documented as individual query parameters. If `TaskFiltersQuery` is moved behind a dependency to make room for `limit`/`cursor`, verify via an OpenAPI test that `date`, `project_ids`, `priorities`, `status`, `recurrence`, `q`, and `include_archived_projects` are still listed individually; a required `filters` object is not acceptable.
- Build the canonical fingerprint mapping exactly as in the spec: `date` as ISO string or `None`, `project_ids` sorted, `priorities` sorted by value, `q` unchanged, `include_archived_projects`, `status`, `recurrence`.
- Decode the cursor to `TaskListCursor` (parse `due_at` with `datetime.fromisoformat`; reject naive values and non-`int` / `bool` `task_id`).
- Call `service.list(..., limit=limit, after=after)`.
- Encode `next_cursor` when present.
- Update the route docstring and error docs if needed.

- [ ] **Step 6: Update fakes and existing tests**

`tests/task_management_fakes.py`:
- Change `list_tasks` to filter/sort by `(due_at, id)` and apply `limit` / cursor.
- Implement the three batch methods.
- Record batch calls in a list for assertions.

`tests/test_tasks_api.py`:
- Change `FakeTaskService.list` to accept `limit`/`after`, sort by `(due_at, id)`, and return `TaskPage`.
- Extend the `_task_detail` helper to accept `task_id` and `due_at`.
- Update assertions that inspect `list_calls`.

`tests/test_task_management_integration.py` and `tests/test_task_management_tasks.py`:
- Pass `limit=DEFAULT_PAGE_SIZE` to existing `service.list(...)` calls.
- Replace `result == []` with `result.items == []`, and `visible` with `page.items`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `poetry run pytest tests/test_tasks_api.py tests/test_task_management_tasks.py tests/test_task_management_repository.py tests/test_task_management_integration.py -q`
Expected: PASS.

- [ ] **Step 8: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/tasks.py src/backend/task_management/ports.py src/backend/task_management/repository.py src/backend/api/routes/tasks.py tests/task_management_fakes.py tests/test_tasks_api.py tests/test_task_management_tasks.py tests/test_task_management_integration.py tests/test_task_management_repository.py
git commit -m "feat: paginate and batch task list"
```

---

### Task 4: Project pagination and batch summaries

**Files:**
- Modify: `src/backend/task_management/projects.py:45-116`
- Modify: `src/backend/task_management/palette.py:33-77`
- Modify: `src/backend/task_management/ports.py:247-430`
- Modify: `src/backend/task_management/repository.py:171-361`
- Modify: `src/backend/api/routes/projects.py:59-146`
- Modify: `tests/task_management_fakes.py`
- Modify: `tests/test_projects_api.py`
- Modify: `tests/test_task_management_projects.py`
- Test: `tests/test_task_management_repository.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ProjectListCursor: is_archived: bool; name_key: str; project_id: int`
  - `@dataclass(frozen=True) class ProjectPage: items: Sequence[ProjectSummary]; next_cursor: ProjectListCursor | None`
  - `ProjectService.list(user_id, include_archived, *, limit, after) -> ProjectPage`
  - `ProjectService.get_summary(user_id, project_id) -> ProjectSummary`
  - `PaletteService.get_colors(color_ids) -> dict[int, ColorPalette]`
  - Repository:
    - `list_projects(..., limit, after_archived, after_name, after_project_id) -> Sequence[Project]`
    - `project_progress_for_projects(user_id, project_ids) -> dict[int, tuple[int, int]]`
    - `get_palette_colors_by_ids(color_ids) -> dict[int, ColorPalette]`
- Consumes: Task 2 cursor codec and page constants.

- [ ] **Step 1: Write failing project pagination tests**

API tests in `tests/test_projects_api.py` (extend `FakeProjectService`):
- `test_project_list_returns_default_page_size` — 51 projects → 50 items + cursor.
- `test_project_list_second_page_has_no_duplicates` — same pattern as tasks.
- `test_project_list_cursor_filter_mismatch_returns_400` — seed 2 projects, request `limit=1`, then change `include_archived` and reuse the non-null cursor.
- `test_project_list_equal_names_uses_id_tiebreaker` — same lower(name), ids in order.
- `test_project_list_rejects_overlong_cursor` — 513 chars → 400 `bad_request`.
- `test_project_list_openapi_documents_individual_params` — `include_archived`, `limit`, `cursor` are individual parameters.
- `test_project_read_uses_single_summary` — fake records `get_summary` call and `list` is not called for `GET /projects/{id}`.
- `test_project_create_uses_single_summary`.

Service tests in `tests/test_task_management_projects.py`:
- `test_list_uses_grouped_progress_and_colors` — fake repository records one progress batch and one color batch call.
- `test_get_summary_returns_progress_and_color`.
- Update existing `service.list(...)` assertions to `page.items`.

Repository tests:
- `test_list_projects_applies_archived_name_id_cursor_and_limit` — compiled SQL contains `lower`, cursor predicates, and `LIMIT`.
- `test_project_progress_for_projects_groups_by_project_id`.
- `test_get_palette_colors_by_ids_uses_in`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_projects_api.py tests/test_task_management_projects.py tests/test_task_management_repository.py -q`
Expected: FAIL because the page dataclasses and batch methods do not exist.

- [ ] **Step 3: Add project domain dataclasses and service methods**

In `task_management/projects.py`:
- Add `ProjectListCursor` and `ProjectPage`.
- Change `list` to accept `limit` and `after`; fetch `limit + 1`, slice, build `next_cursor` from the last kept project (`is_archived = project.archived_at is not None`, `name_key = project.name.lower()`, `project_id = project.id`).
- Use `project_progress_for_projects` and `palette_service.get_colors` for the page.
- Add `get_summary` using `_require_project`, `project_progress`, and `palette_service.get_color`.

- [ ] **Step 4: Implement repository pagination and batch methods**

`list_projects`:
- Add `limit`, `after_archived`, `after_name`, `after_project_id`.
- Use `archived_rank = case((Project.archived_at.is_(None), 0), else_=1)`.
- Apply cursor predicate with `archived_rank`, `func.lower(Project.name)`, `Project.id`.
- Order by `archived_rank`, `func.lower(Project.name)`, `Project.id`; `.limit(limit)`.

`project_progress_for_projects`:
- `select(Task.project_id, func.count(Task.id), func.count(Task.id).filter(Task.completed_at.is_not(None))).where(Task.user_id == user_id, Task.deleted_at.is_(None), Task.project_id.in_(project_ids)).group_by(Task.project_id)`.
- Return `{}` for empty input.

`get_palette_colors_by_ids`:
- `select(ColorPalette).where(ColorPalette.id.in_(color_ids))`; return dict.

- [ ] **Step 5: Wire palette service**

Add `PaletteService.get_colors` that calls the repository and raises `NotFoundError(_COLOR_NOT_FOUND)` if any requested id is missing. Update the class docstring.

- [ ] **Step 6: Wire the route**

In `api/routes/projects.py`:
- Add `limit` and `cursor` query params exactly like tasks: no FastAPI `max_length` on `cursor`; `decode_cursor` owns the 512-char limit and returns 400.
- Keep `include_archived` documented as an individual query parameter alongside `limit`/`cursor`.
- Build fingerprint `{"include_archived": include_archived}`.
- Decode to `ProjectListCursor` (`is_archived` must be `0` or `1`; `name_key` must be `str`; `project_id` must be `int` but not `bool`).
- Call `service.list(..., limit=limit, after=after)`.
- Replace `_load_summary` implementation with `return await service.get_summary(user_id, project_id)`; delete `_find_summary`.
- Update route docstrings.

- [ ] **Step 7: Update fakes and existing tests**

- `FakeProjectService.list` accepts `limit`/`after`, sorts by `(is_archived, lower(name), id)`, and returns `ProjectPage`.
- Add `get_summary` to the fake.
- Extend the project detail helper to accept `project_id`/`name`.
- Update existing test assertions from a list to `page.items`, passing `limit=DEFAULT_PAGE_SIZE` where the service is called directly.
- Update fake repository `list_projects`, add `project_progress_for_projects`, `get_palette_colors_by_ids`, and record batch calls.

- [ ] **Step 8: Run tests to verify they pass**

Run: `poetry run pytest tests/test_projects_api.py tests/test_task_management_projects.py tests/test_task_management_repository.py -q`
Expected: PASS.

- [ ] **Step 9: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/projects.py src/backend/task_management/palette.py src/backend/task_management/ports.py src/backend/task_management/repository.py src/backend/api/routes/projects.py tests/task_management_fakes.py tests/test_projects_api.py tests/test_task_management_projects.py tests/test_task_management_repository.py
git commit -m "feat: paginate and batch project list"
```

---

### Task 5: Series generation selection, bulk writes, index, due-time window

**Files:**
- Modify: `src/backend/task_management/repository.py:1600-1637,1745-1773,887-960,1085-1103`
- Modify: `src/backend/task_management/ports.py:1082-1210`
- Modify: `src/backend/task_management/series.py:467-632`
- Modify: `src/backend/models/task_series.py:38-58`
- Create: `alembic/versions/0004_task_series_generation_idx.py`
- Modify: `tests/task_management_fakes.py`
- Test: `tests/test_task_management_series.py`
- Test: `tests/test_task_management_repository.py`
- Test: `tests/test_task_generation.py`
- Test: `tests/test_task_management_migration.py`

**Interfaces:**
- Produces:
  - `list_series_for_generation(now)` keeps its signature but narrows the query.
  - `add_tasks(tasks: Sequence[Task]) -> Sequence[Task]`
  - `add_task_label_rows(rows: Sequence[TaskLabel]) -> None`
  - `add_reminders(reminders: Sequence[TaskReminder]) -> None`
  - `list_series_due_times(series_id: int, user_id: int, *, now: datetime) -> Sequence[datetime]`
- Consumes: `HORIZON_DAYS`, `localize_wall_time`, existing `_generate_series`.

- [ ] **Step 1: Write failing generation tests**

Repository tests:
- `test_list_series_for_generation_excludes_ended_series` — series with `ends_on` two days ago is not returned.
- `test_list_series_for_generation_excludes_caught_up_series` — `generated_through = now + 89d` is not returned; `now + 88d` is returned.
- `test_list_series_due_times_filters_future_slots` — old and future tasks; only future returned.
- `test_list_series_for_generation_includes_future_series_within_horizon` — `starts_on = now + 10d` is returned.
- `test_list_series_for_generation_skips_far_future_series_after_first_generation` — `generated_through = starts_on` far in the future is not returned.

Service tests:
- `test_generate_series_uses_bulk_add_methods` — fake records `add_tasks`, `add_task_label_rows`, `add_reminders`; assert each called once for several due dates.
- `test_generate_series_does_not_claim_ended_series` — fake suspects query already filters; assert no `add_tasks`.
- `test_generate_series_preserves_generated_through`.

Migration test:
- Load `0004_task_series_generation_idx.py`; assert revision/down_revision; assert the model metadata contains `ix_task_series_generated_through_active`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_series.py tests/test_task_management_repository.py tests/test_task_generation.py tests/test_task_management_migration.py -q`
Expected: FAIL because the query and bulk methods are missing.

- [ ] **Step 3: Narrow the generation query**

In `list_series_for_generation`:
- Compute `threshold = now + timedelta(days=HORIZON_DAYS) - timedelta(days=1)`.
- Compute `utc_today = now.astimezone(UTC).date()`.
- Replace `generated_through < horizon` with `generated_through IS NULL OR generated_through < threshold`.
- Add `ends_on IS NULL OR ends_on >= utc_today - timedelta(days=1)`.
- Keep the active-project predicate and `order_by(TaskSeries.id)`.

- [ ] **Step 4: Add bulk repository methods**

Implement `add_tasks`, `add_task_label_rows`, `add_reminders` with `session.add_all(...)`, a single `await session.flush()`, and return the tasks for `add_tasks`. Keep the existing single-row methods.

- [ ] **Step 5: Use bulk writes in `_generate_series`**

- Build `tasks: list[Task]` for new due times.
- Call `tasks = list(await self._repository.add_tasks(tasks))`.
- Import `TaskLabel` from `backend.models.task_label` and build `TaskLabel` rows from the series labels and all task ids; call `add_task_label_rows` once.
- Build `TaskReminder` rows with `_instance_reminder`; call `add_reminders` once.
- Keep `series.generated_through` logic and the final flush.

- [ ] **Step 6: Add the due-time window**

Change `list_series_due_times` signature to require `now` and add `Task.due_at >= now` to the query. Update the call in `_generate_series`, the Protocol, and the fake.

- [ ] **Step 7: Add model index and migration**

In `models/task_series.py`, add:

```python
Index(
    "ix_task_series_generated_through_active",
    "generated_through",
    postgresql_where=text("deleted_at IS NULL"),
),
```

Create `alembic/versions/0004_task_series_generation_idx.py` with revision `"0004_task_series_generation_idx"`, `down_revision = "0003_task_management"`, `upgrade()` creating the index with `postgresql_where=sa.text("deleted_at IS NULL")`, and `downgrade()` dropping it.

- [ ] **Step 8: Run tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_series.py tests/test_task_management_repository.py tests/test_task_generation.py tests/test_task_management_migration.py -q`
Expected: PASS.

- [ ] **Step 9: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/repository.py src/backend/task_management/ports.py src/backend/task_management/series.py src/backend/models/task_series.py alembic/versions/0004_task_series_generation_idx.py tests/task_management_fakes.py tests/test_task_management_series.py tests/test_task_management_repository.py tests/test_task_generation.py tests/test_task_management_migration.py
git commit -m "perf: bound and batch series generation"
```

---

### Task 6: Batch unarchive reminders

**Files:**
- Modify: `src/backend/task_management/repository.py:1293-1368`
- Test: `tests/test_task_management_repository.py`
- Test: `tests/test_task_management_projects.py`

**Interfaces:**
- `resume_project_reminders(user_id, project_id, now)` keeps its signature and behavior.
- No new public methods.

- [ ] **Step 1: Write failing query-shape tests**

Add to `tests/test_task_management_repository.py`:

```python
async def test_resume_project_reminders_batches_series_and_task_reminders(recording_session):
    repository = SqlAlchemyTaskManagementRepository(recording_session)
    await repository.resume_project_reminders(USER_ID, PROJECT_ID, NOW)
    statements = "\n".join(str(statement) for statement in recording_session.statements)
    assert statements.count("FROM task_reminders") == 2
    assert "IN (" in statements
    assert "task_reminders.series_id IN" in statements
    assert "task_reminders.task_id IN" in statements
```

Add a service-level test that unarchiving a project with two series and two tasks still mutates all reminder slots.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_repository.py tests/test_task_management_projects.py -q`
Expected: FAIL because reminders are queried per series/task.

- [ ] **Step 3: Batch the queries**

In `resume_project_reminders`:
- Load series as today.
- If `series_ids`, load all reminders with `TaskReminder.series_id.in_(series_ids)` and group by `series_id`.
- Load incomplete tasks as today.
- If `task_ids`, load all reminders with `TaskReminder.task_id.in_(task_ids)` and group by `task_id`.
- Apply the same `next_run_at` / `timezone` mutations.
- Keep one final `flush()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_repository.py tests/test_task_management_projects.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/repository.py tests/test_task_management_repository.py tests/test_task_management_projects.py
git commit -m "perf: batch unarchive reminder queries"
```

---

### Task 7: Reminder dispatch limit and deadline

**Files:**
- Modify: `src/backend/task_management/reminders.py:24-30,364-499`
- Modify: `src/backend/task_management/repository.py:1775-1838`
- Modify: `src/backend/task_management/ports.py`
- Modify: `tests/task_management_fakes.py`
- Test: `tests/test_task_management_reminders.py`
- Test: `tests/test_reminder_delivery.py`

**Interfaces:**
- Produces:
  - `_DISPATCH_BATCH_SIZE = 100`
  - `_DISPATCH_TIMEOUT_SECONDS = 240.0`
  - `list_due_reminders(now: datetime, *, limit: int) -> Sequence[TaskReminder]`
  - `ReminderDispatcher(..., monotonic: Callable[[], float] = time.monotonic)`
- Behavior: check the deadline before each reminder; leave the rest for the next run.

- [ ] **Step 1: Write failing dispatcher tests**

```python
async def test_dispatch_due_passes_batch_limit(...):
    await dispatcher.dispatch_due()
    assert repository.due_reminder_limits == [100]

async def test_dispatch_due_stops_at_deadline(...):
    # First call starts the timer at 0; every candidate check then sees 240.
    ticks = iter([0.0] + [240.0] * 10)
    dispatcher = ReminderDispatcher(..., monotonic=lambda: next(ticks))
    await dispatcher.dispatch_due()
    assert sender.sent == []

async def test_dispatch_due_finishes_in_flight_send(...):
    # Timer starts at 0; the first candidate check is before 240 (send starts);
    # the next check sees 250 and stops, so exactly one send completes.
    ticks = iter([0.0, 239.9, 250.0, 250.0])
    dispatcher = ReminderDispatcher(..., monotonic=lambda: next(ticks))
    await dispatcher.dispatch_due()
    assert sender.sent == [first_reminder_id]
```

Repository test: `list_due_reminders` compiled SQL contains `LIMIT`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_reminders.py tests/test_reminder_delivery.py -q`
Expected: FAIL because the limit/deadline are missing.

- [ ] **Step 3: Implement the limit and deadline**

- Add the constants.
- Add `limit` to `list_due_reminders` and `.limit(limit)` to the query.
- Add `monotonic` to `ReminderDispatcher.__init__`.
- In `dispatch_due`, record `started = self._monotonic()` and check
  `if self._monotonic() - started >= _DISPATCH_TIMEOUT_SECONDS: break`
  at the start of each candidate iteration.
- Pass `_DISPATCH_BATCH_SIZE` to the repository call.
- Keep all existing commit/lease/backoff behavior.

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_reminders.py tests/test_reminder_delivery.py -q`
Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/reminders.py src/backend/task_management/repository.py src/backend/task_management/ports.py tests/task_management_fakes.py tests/test_task_management_reminders.py tests/test_reminder_delivery.py
git commit -m "perf: bound reminder dispatch batch and runtime"
```

---

### Task 8: Database timeouts

**Files:**
- Modify: `src/backend/core/config.py:36-41`
- Modify: `src/backend/db/session.py:34-46`
- Modify: `.env.example`
- Modify: `compose.yml:18-23`
- Modify: `README.md:482-494`
- Test: `tests/test_config.py`
- Test: `tests/test_db_session.py` (new)

**Interfaces:**
- Produces:
  - `Settings.db_statement_timeout_ms: int`
  - `Settings.db_pool_timeout_seconds: int`
- Engine receives `pool_timeout`, `connect_args["command_timeout"]`, and `server_settings["statement_timeout"]`.

- [ ] **Step 1: Write failing config/session tests**

`tests/test_config.py`:

```python
def test_db_timeout_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.db_statement_timeout_ms == 30_000
    assert settings.db_pool_timeout_seconds == 10

def test_db_timeout_overrides(monkeypatch) -> None:
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "5000")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "3")
    settings = Settings(_env_file=None)
    assert settings.db_statement_timeout_ms == 5000
    assert settings.db_pool_timeout_seconds == 3
```

`tests/test_db_session.py`:

```python
def test_database_engine_receives_timeouts(monkeypatch) -> None:
    captured = {}
    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()
    monkeypatch.setattr("backend.db.session.create_async_engine", fake_create_async_engine)
    monkeypatch.setattr("backend.db.session.async_sessionmaker", lambda *args, **kwargs: object())
    Database(Settings(_env_file=None))
    assert captured["pool_timeout"] == 10
    assert captured["connect_args"]["command_timeout"] == 30.0
    assert captured["connect_args"]["server_settings"]["statement_timeout"] == "30000"

def test_database_engine_receives_overridden_timeouts(monkeypatch) -> None:
    captured = {}
    def fake_create_async_engine(url, **kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr("backend.db.session.create_async_engine", fake_create_async_engine)
    monkeypatch.setattr("backend.db.session.async_sessionmaker", lambda *args, **kwargs: object())
    Database(Settings(_env_file=None, db_statement_timeout_ms=1500, db_pool_timeout_seconds=3))
    assert captured["pool_timeout"] == 3
    assert captured["connect_args"]["command_timeout"] == 1.5
    assert captured["connect_args"]["server_settings"]["statement_timeout"] == "1500"
```

Add a `gt=0` rejection test for both settings using Pydantic `ValidationError`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_config.py tests/test_db_session.py -q`
Expected: FAIL because the settings and engine arguments are missing.

- [ ] **Step 3: Implement settings and engine wiring**

- Add both fields to `Settings` with `Field(default=..., gt=0)`.
- Add `pool_timeout=settings.db_pool_timeout_seconds`.
- Add `command_timeout=settings.db_statement_timeout_ms / 1000`.
- Add `"statement_timeout": str(settings.db_statement_timeout_ms)` to `server_settings`.
- Update docstrings.

- [ ] **Step 4: Update env/deployment examples**

Add to `.env.example`:

```dotenv
DB_STATEMENT_TIMEOUT_MS=30000
DB_POOL_TIMEOUT_SECONDS=10
```

Add to the `compose.yml` app environment block:

```yaml
DB_STATEMENT_TIMEOUT_MS: ${DB_STATEMENT_TIMEOUT_MS:-30000}
DB_POOL_TIMEOUT_SECONDS: ${DB_POOL_TIMEOUT_SECONDS:-10}
```

`DATABASE_URL` goes through PgBouncer. PgBouncer rejects unknown startup parameters, so add `statement_timeout` to the existing PgBouncer ignore list:

```yaml
IGNORE_STARTUP_PARAMETERS: extra_float_digits,statement_timeout
```

This keeps connections working; the server-side `statement_timeout` is not applied through transaction pooling, while asyncpg `command_timeout` remains the effective client-side bound. Document this fallback in the README timeout note.

Add the two variables to the README PostgreSQL env list.

- [ ] **Step 5: Run tests to verify they pass**

Run: `poetry run pytest tests/test_config.py tests/test_db_session.py -q`
Expected: PASS.

- [ ] **Step 6: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/core/config.py src/backend/db/session.py .env.example compose.yml README.md tests/test_config.py tests/test_db_session.py
git commit -m "feat: add database statement and pool timeouts"
```

---

### Task 9: Input limits and series label batch validation

**Files:**
- Modify: `src/backend/api/task_schemas.py:36-37,68-69,431-436,490-495,605-613,692-700`
- Modify: `src/backend/task_management/labels.py:63-86`
- Modify: `src/backend/task_management/series.py:842-871`
- Modify: `tests/task_management_fakes.py`
- Test: `tests/test_tasks_api.py`
- Test: `tests/test_task_series_api.py`
- Test: `tests/test_projects_api.py`
- Test: `tests/test_task_management_series.py`

**Interfaces:**
- `description` request fields: `max_length=5000`.
- `label_ids` request fields: `max_length=100`.
- `LabelService.list_by_ids(user_id, label_ids) -> Sequence[Label]`.
- `SeriesService._validate_labels` dedupes, calls `list_by_ids` once, and raises `NotFoundError` when counts differ.

- [ ] **Step 1: Write failing tests**

API tests:

```python
async def test_task_create_rejects_too_many_labels(task_client):
    response = await task_client.post("/api/v1/tasks", json={
        "title": "Many", "due_at": "2026-11-04T09:00:00Z",
        "label_ids": list(range(1, 102)),
    })
    assert response.status_code == 422

async def test_series_create_rejects_too_many_labels(series_client):
    response = await series_client.post("/api/v1/task-series", json={
        "title": "Many", "time_of_day": "09:00:00", "timezone": "UTC",
        "starts_on": "2026-11-04", "label_ids": list(range(1, 102)),
    })
    assert response.status_code == 422

async def test_task_create_rejects_too_long_description(task_client):
    response = await task_client.post("/api/v1/tasks", json={
        "title": "Long", "due_at": "2026-11-04T09:00:00Z",
        "description": "x" * 5001,
    })
    assert response.status_code == 422
```

Service test:

```python
async def test_series_label_validation_uses_one_batch_query(fake_repository):
    ...
    await service.create(..., label_ids=(1, 1, 2))
    assert fake_repository.label_batch_calls == [((1, 2),)]  # deduped, one call
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_tasks_api.py tests/test_task_series_api.py tests/test_projects_api.py tests/test_task_management_series.py -q`
Expected: FAIL because the limits and batch validation are missing.

- [ ] **Step 3: Add Pydantic limits**

Add `max_length=5000` to every request `description` field and
`max_length=100` to every request `label_ids` field listed in the spec.
Keep response models unchanged.

- [ ] **Step 4: Add `LabelService.list_by_ids`**

Delegate to `repository.get_labels_by_ids`. Add a full docstring.

- [ ] **Step 5: Batch series label validation**

Replace the per-label loop with:

```python
unique_ids = tuple(dict.fromkeys(label_ids))
labels = await self._label_service.list_by_ids(user_id, unique_ids)
if len(labels) != len(unique_ids):
    raise NotFoundError(_LABEL_NOT_FOUND)
return unique_ids
```

Update imports and docstring.

- [ ] **Step 6: Update the fake repository**

Ensure `get_labels_by_ids` records calls and returns non-deleted owned labels. Add `label_batch_calls` if the existing fake does not expose it.

- [ ] **Step 7: Run tests to verify they pass**

Run: `poetry run pytest tests/test_tasks_api.py tests/test_task_series_api.py tests/test_projects_api.py tests/test_task_management_series.py -q`
Expected: PASS.

- [ ] **Step 8: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/api/task_schemas.py src/backend/task_management/labels.py src/backend/task_management/series.py tests/task_management_fakes.py tests/test_tasks_api.py tests/test_task_series_api.py tests/test_projects_api.py tests/test_task_management_series.py
git commit -m "feat: bound descriptions and labels, batch series labels"
```

---

### Task 10: Critical tests, coverage tooling, docs, final verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md:465-480`
- Create: `tests/test_route_auth_matrix.py`
- Create: `tests/test_cross_user_access.py`
- Create: `tests/test_auth_http_security.py`
- Create: `tests/test_auth_repository.py`
- Modify: `tests/test_auth_tokens.py`
- Modify: `tests/test_task_management_repository.py`

**Interfaces:**
- Dev dependency `pytest-cov (>=7.0,<8.0)`.
- Coverage command: `poetry run pytest --cov=backend --cov-report=term-missing`.
- No coverage threshold.

- [ ] **Step 1: Add pytest-cov**

Run:

```bash
poetry add --group dev "pytest-cov>=7.0,<8.0"
```

Expected: `pyproject.toml` and `poetry.lock` updated.

- [ ] **Step 2: Write the route-auth matrix test**

Create `tests/test_route_auth_matrix.py`:

```python
PUBLIC_PATHS = {
    "/health/live",
    "/health/ready",
    "/api/v1/auth/telegram",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/openapi.json",
    "/docs",
    "/redoc",
    "/docs/oauth2-redirect",
}

def _protected_operations(app):
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in PUBLIC_PATHS or route.path.startswith("/admin"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            yield method, route.path

@pytest.mark.parametrize("method,path", list(_protected_operations(app)))
async def test_protected_route_requires_bearer(method, path, client):
    url = path
    for param in re.findall(r"\{[^}]+\}", path):
        url = url.replace(param, "1")
    response = await client.request(method, url)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
```

`app` must come from the `create_app(settings)` fixture with docs enabled if needed. If the parametrization cannot use the fixture, build the app at module scope with test settings and clear dependency overrides.

If any new test in this task exposes a real authorization defect, stop and report it before changing auth/authorization behavior. The review found the isolation intact; these tests are regression guards, not a license to refactor auth.

- [ ] **Step 3: Write cross-user body ID tests**

Create `tests/test_cross_user_access.py`. Use `FakeTaskManagementRepository` with two users or the API fakes. Cover:

- create task with another user's `project_id` → 404, no task;
- create task with another user's `label_ids` → 404, no task;
- create series with another user's `project_id`/`label_ids` → 404, no series;
- update task/series with another user's IDs → 404, no mutation;
- complete/uncomplete another user's task → 404;
- archive/unarchive another user's project → 404;
- delete another user's label is a no-op 204 (existing behavior) and does not delete it.

Assert the repository state is unchanged after each failure.

- [ ] **Step 4: Write HTTP auth security tests**

Create `tests/test_auth_http_security.py`:

- `test_me_rejects_malformed_bearer` — `Authorization: Bearer not-a-jwt` → 401.
- `test_me_rejects_expired_token` — create a token with `now` in the past beyond TTL → 401.
- `test_tasks_rejects_wrong_secret_token` — sign with another secret → 401.
- `test_refresh_rejects_disallowed_origin` — `Origin: https://evil.example` → 403.
- `test_logout_rejects_disallowed_origin` — same.
- `test_refresh_without_origin_reaches_cookie_check` — no Origin and no cookie → 401, not 403.

Use `Settings(_env_file=None, auth_jwt_secret="test-secret-...")` and override `get_auth_service` where needed.

- [ ] **Step 5: Add JWT claim hardening tests**

Extend `tests/test_auth_tokens.py` with:

- missing each required claim (`exp`, `iat`, `iss`, `sub`, `sid`, `telegram_id`, `jti`, `type`) → `UnauthorizedError`;
- invalid `sid`, `telegram_id`, `jti` types → `UnauthorizedError`;
- `alg=none` and `alg=RS256` tokens → `UnauthorizedError`;
- non-numeric/overflow `iat`/`exp` → `UnauthorizedError`.

- [ ] **Step 6: Add auth repository SQL tests**

Create `tests/test_auth_repository.py` using a recording session helper (copy the pattern from `tests/test_task_management_repository.py`). Cover:

- `get_session_for_update` SQL contains `FOR UPDATE` and `refresh_token_hash = :...`;
- `revoke_user_sessions` filters by `user_id`;
- `revoke_family` filters by `family_id`;
- `revoke_session_and_commit`/`revoke_family_and_commit` commit after the update.

- [ ] **Step 7: Add tenant-scope repository tests**

Extend `tests/test_task_management_repository.py`:

- `get_task`, `list_tasks`, `get_series`, `list_series`, `soft_delete_task`, `add_task_labels` all include `user_id` in compiled SQL.
- New pagination and batch methods include `user_id` and `IN`.

- [ ] **Step 8: Update README testing section**

Add:

```bash
poetry run pytest --cov=backend --cov-report=term-missing
```

State that coverage has no enforced threshold and is a local diagnostic.
Document the pagination contract:
- `GET /tasks` and `GET /projects` return `{items, next_cursor}`;
- default limit 50, max 100;
- pass `next_cursor` as `cursor` without changing filters.

- [ ] **Step 9: Run the full verification**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
poetry run pytest --cov=backend --cov-report=term-missing
graphify update .
```

Expected: all green; coverage report is produced; graphify updates without errors.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml poetry.lock README.md tests/test_route_auth_matrix.py tests/test_cross_user_access.py tests/test_auth_http_security.py tests/test_auth_repository.py tests/test_auth_tokens.py tests/test_task_management_repository.py
git commit -m "test: add critical coverage and document pagination"
```

---

## Self-Review Notes

- Spec coverage: every numbered goal maps to a task: pagination (2–4), batch tasks/projects (3–4), series (5), unarchive (6), dates (1), limits (9), reminders (7), DB timeouts (8), tests/coverage (10).
- Type consistency: cursor dataclasses and repository signatures are defined in the task where they first appear and reused by later tasks.
- Breaking changes are called out in Task 3/4/10 and the README update.
- Out-of-scope items from the spec (auth, CORS, compose secrets/ports, series list pagination, search index) are not touched.
