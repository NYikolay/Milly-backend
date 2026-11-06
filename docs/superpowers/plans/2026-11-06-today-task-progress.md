# Today Task Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `GET /api/v1/tasks/today/progress` returning the authenticated user's total, completed, and remaining tasks due in the user's local day.

**Architecture:** A new `TaskService.today_progress` resolves the user's timezone, converts the current local calendar day into UTC bounds through the existing `localize_wall_time` helper, and delegates counting to the existing repository method `today_progress`. The API route serializes the `(total, completed)` tuple into a flat response with `remaining = total - completed`.

**Tech Stack:** Python 3.14, FastAPI 0.156.x, Pydantic 2.x, SQLAlchemy 2.x, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-03-task-management-database-design.md` §9.11 and §10.

## Global Constraints

- All API errors use the unified error envelope; raise `BadRequestError` / `UnauthorizedError` rather than returning ad-hoc errors.
- `TaskService` never commits; the request unit of work owns the transaction.
- Docstrings are mandatory for every new or changed function and must match the code.
- No new runtime or dev dependencies.
- "Today" means the user's local calendar day `[00:00, next 00:00)`, converted to UTC; an unset timezone defaults to UTC.
- `completed` counts tasks due today whose `completed_at IS NOT NULL`, regardless of the completion instant.
- Files deleted with `deleted_at` are excluded.
- New API routers/routes use the existing `StandardizedAPIRouter` and `document_errors`.
- Verification commands: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`, `graphify update .`.

## Review Focus

- An invalid stored timezone must produce `400 bad_request`, not a 500 — covered by `test_today_progress_rejects_invalid_user_timezone`.
- A user without a stored timezone must use UTC — covered by `test_today_progress_defaults_to_utc_when_timezone_unset`.
- Due instants exactly on the local midnight boundaries must be start-inclusive and end-exclusive — covered by `test_today_progress_uses_local_day_bounds_and_completed_state`.
- Soft-deleted tasks due today must not be counted — covered by `test_today_progress_uses_local_day_bounds_and_completed_state`.
- A task due today but completed earlier must still count as completed, because the spec counts completion state rather than the completion instant — covered by `test_today_progress_uses_local_day_bounds_and_completed_state`.

---

### Task 1: Service-level today progress

**Files:**
- Modify: `src/backend/task_management/tasks.py` (add public `today_progress`; add private `_local_day_bounds`; refactor the date filter in `list` to use the helper)
- Test: `tests/test_task_management_tasks.py`

**Interfaces:**
- Consumes: `TaskManagementRepository.today_progress(user_id: int, start: datetime, end: datetime) -> tuple[int, int]` (already implemented in `src/backend/task_management/repository.py` and the fake repository), `TaskService._user_timezone(user_id: int) -> ZoneInfo`, `TaskService._clock() -> datetime`, `backend.task_management.schedule.localize_wall_time(local_date: date, time_of_day: time, timezone: ZoneInfo) -> datetime`.
- Produces: `TaskService.today_progress(user_id: int) -> tuple[int, int]`; private `TaskService._local_day_bounds(zone: ZoneInfo, local_date: date) -> tuple[datetime, datetime]`.

- [ ] **Step 1: Write the failing service tests**

Add these tests to `tests/test_task_management_tasks.py` (keep imports consistent with the existing file; it already imports `UTC`, `datetime`, `pytest`, `BadRequestError`, `ErrorCode`, and the fake factories):

```python
async def test_today_progress_uses_local_day_bounds_and_completed_state() -> None:
    """Today progress uses the user's local day and completion timestamps."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Europe/Berlin"
    repository.insert_task(
        make_task(
            task_id=1,
            due_at=datetime(2026, 11, 3, 23, 0, tzinfo=UTC),
            completed_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        )
    )
    repository.insert_task(
        make_task(task_id=2, due_at=datetime(2026, 11, 4, 22, 59, tzinfo=UTC))
    )
    repository.insert_task(
        make_task(task_id=3, due_at=datetime(2026, 11, 4, 23, 0, tzinfo=UTC))
    )
    repository.insert_task(
        make_task(
            task_id=4,
            due_at=datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
            deleted_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
        )
    )
    service = make_service(repository)

    result = await service.today_progress(USER_ID)

    assert result == (2, 1)


async def test_today_progress_defaults_to_utc_when_timezone_unset() -> None:
    """An unset user timezone falls back to UTC day bounds."""
    repository = FakeTaskManagementRepository()
    repository.insert_task(
        make_task(
            task_id=1,
            due_at=datetime(2026, 11, 4, 0, 0, tzinfo=UTC),
            completed_at=datetime(2026, 11, 4, 8, 0, tzinfo=UTC),
        )
    )
    repository.insert_task(
        make_task(task_id=2, due_at=datetime(2026, 11, 4, 23, 59, tzinfo=UTC))
    )
    repository.insert_task(
        make_task(task_id=3, due_at=datetime(2026, 11, 5, 0, 0, tzinfo=UTC))
    )
    service = make_service(repository)

    assert await service.today_progress(USER_ID) == (2, 1)


async def test_today_progress_rejects_invalid_user_timezone() -> None:
    """An invalid stored timezone is a bad request."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Invalid/Zone"
    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.today_progress(USER_ID)

    assert error.value.code is ErrorCode.BAD_REQUEST
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py -k today_progress -v`

Expected: FAIL with `AttributeError: 'TaskService' object has no attribute 'today_progress'`.

- [ ] **Step 3: Implement the service method and shared day-bounds helper**

In `src/backend/task_management/tasks.py`:

1. Refactor the date branch of `TaskService.list` to call the new private helper instead of inlining `localize_wall_time` calls:

```python
if filters.date is not None:
    zone = await self._user_timezone(user_id)
    date_start, date_end = self._local_day_bounds(zone, filters.date)
```

2. Add private helper with this exact signature:

```python
def _local_day_bounds(
    self,
    zone: ZoneInfo,
    local_date: date,
) -> tuple[datetime, datetime]:
```

It must call `localize_wall_time(local_date, time(0, 0), zone)` for the inclusive start and `localize_wall_time(local_date + timedelta(days=1), time(0, 0), zone)` for the exclusive end, translating `OverflowError` into `BadRequestError(_INVALID_DATE_RANGE)`. Do not add timezone validation to the helper; `_user_timezone` owns it.

3. Add public method with this exact signature:

```python
async def today_progress(self, user_id: int) -> tuple[int, int]:
```

It must resolve the user timezone with `await self._user_timezone(user_id)`, derive the local date as `self._clock().astimezone(zone).date()`, obtain bounds from `_local_day_bounds`, and return `await self._repository.today_progress(user_id, start, end)`.

4. Write docstrings for both methods per `AGENTS.md`: purpose, args, returns, raises (`BadRequestError` for invalid timezone or out-of-range bounds), and notes (local day converted to UTC; no writes/commit).

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_tasks.py -q`

Expected: all tests pass, including the three new `today_progress` tests.

- [ ] **Step 5: Run lint and type checks on the changed files**

Run: `poetry run ruff check src/backend/task_management/tasks.py tests/test_task_management_tasks.py && poetry run ruff format --check src/backend/task_management/tasks.py tests/test_task_management_tasks.py && poetry run mypy src/backend/task_management/tasks.py tests/test_task_management_tasks.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/backend/task_management/tasks.py tests/test_task_management_tasks.py
git commit -m "feat: add today task progress service method"
```

Do not stage unrelated working-tree changes.

---

### Task 2: API endpoint and documentation

**Files:**
- Modify: `src/backend/api/task_schemas.py` (add `TodayProgressResponse`)
- Modify: `src/backend/api/routes/tasks.py` (add `GET /today/progress`)
- Modify: `tests/test_tasks_api.py` (extend `FakeTaskService` and add the route test)
- Modify: `README.md` (endpoint table)
- Modify: `docs/api.md` (operation table and endpoint section)

**Interfaces:**
- Consumes: `TaskService.today_progress(user_id: int) -> tuple[int, int]` from Task 1; `CurrentAccessDep`; `TaskServiceDep`; `StandardizedAPIRouter`; `document_errors`.
- Produces: `TodayProgressResponse(total: int, completed: int, remaining: int)`; route `tasks-read_today_progress` at `GET /api/v1/tasks/today/progress`.

- [ ] **Step 1: Extend the API test double and write the failing route test**

In `tests/test_tasks_api.py`:

1. Extend `FakeTaskService.__init__` with:

```python
self.today_progress_result: tuple[int, int] = (0, 0)
self.today_progress_calls: list[int] = []
```

2. Add this method to `FakeTaskService`:

```python
async def today_progress(self, user_id: int) -> tuple[int, int]:
    """Record the user and return prepared today progress counts."""
    self.today_progress_calls.append(user_id)
    return self.today_progress_result
```

3. Add this test:

```python
async def test_read_today_progress_returns_counts(
    task_client: AsyncClient,
    fake_task_service: FakeTaskService,
) -> None:
    """Return total, completed, and remaining tasks due today."""
    fake_task_service.today_progress_result = (5, 2)

    response = await task_client.get("/api/v1/tasks/today/progress")

    assert response.status_code == 200
    assert response.json() == {"total": 5, "completed": 2, "remaining": 3}
    assert fake_task_service.today_progress_calls == [1]
```

- [ ] **Step 2: Run the new route test to verify it fails**

Run: `poetry run pytest tests/test_tasks_api.py -k today_progress -v`

Expected: FAIL with `404` because the route does not exist yet.

- [ ] **Step 3: Implement the schema and route**

In `src/backend/api/task_schemas.py`, add:

```python
class TodayProgressResponse(BaseModel):
    """Task counts for the user's local day."""
```

with integer fields `total`, `completed`, and `remaining`. Write its one-line class docstring.

In `src/backend/api/routes/tasks.py`:

1. Import `TodayProgressResponse`.
2. Add the static route before `/{task_id}` (after `list_tasks`), using `StandardizedAPIRouter` conventions:

```python
@router.get(
    "/today/progress",
    response_model=TodayProgressResponse,
    responses=document_errors(_ERRORS),
)
async def read_today_progress(
    claims: CurrentAccessDep,
    service: TaskServiceDep,
) -> TodayProgressResponse:
```

The body must call `await service.today_progress(claims.user_id)`, compute `remaining = total - completed`, and return the schema. The docstring must document the purpose, args, returns, raises (`UnauthorizedError` 401, `BadRequestError` 400 for an invalid stored timezone), and the local-day semantics.

- [ ] **Step 4: Run the route test and the task API suite**

Run: `poetry run pytest tests/test_tasks_api.py -q`

Expected: all tests pass, including `test_read_today_progress_returns_counts`.

- [ ] **Step 5: Update documentation**

In `README.md`, add a row to the task endpoints table:

```markdown
| `GET` | `/api/v1/tasks/today/progress` | количество задач на сегодня и выполненных |
```

In `docs/api.md`:

1. Add this row to the operation ID table:

```markdown
| `tasks-read_today_progress` | `GET /api/v1/tasks/today/progress` |
```

2. Add a new section at the end of section 9, before `## 10`, titled:

```markdown
### 9.9. `GET /api/v1/tasks/today/progress`
```

Document: Bearer auth, operationId, purpose, the `200` response shape `{"total": 5, "completed": 2, "remaining": 3}`, and semantics: `total` counts non-deleted tasks with `due_at` in the user's local day `[00:00, next 00:00)`; `completed` is the subset with `completed_at != null`; `remaining = total - completed`; an unset timezone defaults to UTC. Document errors `400 bad_request` (invalid stored timezone) and `401 unauthorized`.

- [ ] **Step 6: Run lint, formatting, and type checks**

Run: `poetry run ruff check . && poetry run ruff format --check src tests && poetry run mypy src tests`

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/backend/api/task_schemas.py src/backend/api/routes/tasks.py tests/test_tasks_api.py README.md docs/api.md
git commit -m "feat: expose today task progress endpoint"
```

Do not stage unrelated working-tree changes.

---

## Final Verification

After both tasks are reviewed and complete, run the full required verification:

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
```
