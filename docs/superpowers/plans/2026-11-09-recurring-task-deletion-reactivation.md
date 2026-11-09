# Recurring Task Deletion and Reactivation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hard-delete future instances when a task of an active series or a series itself is deleted, and recreate the series when a completed task of a soft-deleted series is reopened outside an active series, rejecting past-date reopenings where the date is editable.

**Architecture:** Extend the existing task-management services rather than adding new tables. `SeriesService.delete` becomes the single owner of series-wide deletion: it hard-deletes future incomplete instances and optionally soft-deletes the selected instance. `TaskService.delete` delegates every active-series deletion to it. `TaskService.uncomplete` gains a local-date guard for non-active-series tasks and recreates a soft-deleted series through the existing `SeriesService.create_from_task` snapshot path. A narrowly scoped repository helper protects the `(series_id, due_at)` unique slot when a completed task keeps its historical series link across a due-date change.

**Tech Stack:** Python 3.14, FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL, pytest, zoneinfo.

**Spec:** `docs/superpowers/specs/2026-11-09-recurring-task-deletion-reactivation-design.md`

## Global Constraints

- No new runtime or dev dependencies.
- No database migration: no new tables, columns, or indexes.
- Hard delete applies only to series tasks with `completed_at IS NULL AND due_at > now`.
- Completed instances are never hard-deleted. A selected completed or overdue instance is soft-deleted.
- Any `DELETE /api/v1/tasks/{task_id}` for a task of an active series deletes the series (soft) and hard-deletes its future incomplete instances.
- One-off tasks and tasks of soft-deleted series keep individual soft-delete behavior.
- `DELETE /api/v1/task-series/{series_id}` uses the same hard-delete rule.
- The past-date guard in `uncomplete` applies only to completed tasks without an active series: one-off tasks and tasks of soft-deleted series.
- Past date means `due_at` local date `<` current local date in `users.timezone`; equal local dates are allowed. UTC is the fallback timezone.
- Error contract: HTTP `400`, code `bad_request`, message `Task due date is in the past`, description `Нельзя вернуть в работу задачу с прошедшей датой. Сначала измените дату задачи.`
- Recreating a series copies the old series `frequency` and `interval_days`; `SeriesService.create_from_task` supplies the rest of the snapshot. The new series has `ends_on = NULL`.
- A completed task of a soft-deleted series keeps `series_id` when its `due_at` changes without an explicit `recurrence`, unless another non-deleted task of that series already occupies the new slot.
- Lock order stays series before tasks in every series-wide operation.
- Every new or changed function/class gets an English docstring in the project template; update stale docstrings in the same change.
- Do not stage unrelated pre-existing working-tree changes (deleted plan files and admin changes).
- Verify after each task: `poetry run pytest -q`, `poetry run ruff check .`, `poetry run ruff format --check src tests`, `poetry run mypy src tests`.
- After the final task: `graphify update .`.

## Review Focus

- Deleting a completed or overdue active-series instance must hard-delete all future incomplete siblings while still deleting the selected instance and the series.
- Reopening a past-dated one-off or soft-deleted-series task must return the exact `400 bad_request` envelope and leave `completed_at` and reminders unchanged.
- Reopening a past-dated active-series instance must succeed, because its date is not editable per instance.
- Reopening a completed task of a soft-deleted series must create a new active series with the old `frequency`/`interval_days` and generate future instances without duplicating the current slot.
- Changing `due_at` on a completed soft-deleted-series task must never violate `uq_tasks_series_id_due_at`; the historical link survives unless the new slot would collide.

---

### Task 1: Hard-delete future instances when a series is deleted

**Files:**
- Modify: `src/backend/task_management/series.py:625-665`
- Modify: `src/backend/task_management/ports.py:812-835` (docstring only)
- Modify: `src/backend/task_management/repository.py:1219-1250` (docstring only)
- Test: `tests/test_task_management_series.py:290-340`, `tests/test_task_management_series.py:775-812`

**Interfaces:**
- Consumes: `TaskManagementRepository.get_series_tasks_for_update`, `cancel_task_reminders`, `hard_delete_tasks`, `soft_delete_task`, `cancel_series_reminders`, `soft_delete_series`.
- Produces: `SeriesService.delete(self, user_id: int, series_id: int, *, target_task_id: int | None = None) -> None`.

- [ ] **Step 1: Rewrite the existing future-instance deletion test**

Replace `test_delete_series_soft_deletes_future_incomplete_and_keeps_past` with:

```python
async def test_delete_series_hard_deletes_future_incomplete_and_keeps_past() -> None:
    """Deleting a series physically removes future incomplete instances."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    past = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=3,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=2),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    await service.delete(USER_ID, series.id)

    assert series.deleted_at == fixed_now()
    assert future.id not in repository.tasks
    assert past.deleted_at is None
    assert completed.deleted_at is None
```

- [ ] **Step 2: Add a selected-instance test**

Append:

```python
async def test_delete_series_soft_deletes_selected_completed_task() -> None:
    """A selected completed instance is soft-deleted while future instances are removed."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    completed = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    future = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=2),
        )
    )
    service = make_service(repository)

    await service.delete(USER_ID, series.id, target_task_id=completed.id)

    assert completed.deleted_at == fixed_now()
    assert completed.deletion_batch_id == series.deletion_batch_id
    assert future.id not in repository.tasks
    assert series.deleted_at == fixed_now()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_series.py::test_delete_series_hard_deletes_future_incomplete_and_keeps_past tests/test_task_management_series.py::test_delete_series_soft_deletes_selected_completed_task -v`

Expected: FAIL because `delete` does not accept `target_task_id` and soft-deletes the future task.

- [ ] **Step 4: Implement the series deletion rule**

In `SeriesService.delete`, add `*, target_task_id: int | None = None`, then:

```python
series = await self._require_series(user_id, series_id, for_update=True)
now = self._clock()
batch_id = uuid4()
locked_tasks = await self._repository.get_series_tasks_for_update(
    series.id,
    user_id,
)
for task in locked_tasks:
    await self._repository.cancel_task_reminders(user_id, task.id)
future_ids = tuple(
    task.id
    for task in locked_tasks
    if task.completed_at is None and task.due_at > now
)
await self._repository.hard_delete_tasks(future_ids, user_id)
if target_task_id is not None and target_task_id not in future_ids:
    target = next(
        (task for task in locked_tasks if task.id == target_task_id),
        None,
    )
    if target is not None:
        await self._repository.soft_delete_task(target, now, batch_id)
await self._repository.cancel_series_reminders(user_id, series.id)
await self._repository.soft_delete_series(series, now, batch_id)
await self._repository.flush()
```

Update the docstring to state that future incomplete instances are physically deleted, the selected instance is soft-deleted when it survives, and completed/past history is preserved.

Also update the `hard_delete_tasks` docstrings in `ports.py` and
`repository.py` so their purpose mentions both series edits and series
deletion, not only edits.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_series.py -q`

Expected: PASS.

- [ ] **Step 6: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/series.py src/backend/task_management/ports.py src/backend/task_management/repository.py tests/test_task_management_series.py
git commit -m "feat: hard delete future tasks when deleting a series"
```

---

### Task 2: Delete the series from any active-series task deletion

**Files:**
- Modify: `src/backend/task_management/tasks.py:772-810`
- Test: `tests/test_task_management_tasks.py:405-520`, `tests/test_task_management_tasks.py:1510-1556`

**Interfaces:**
- Consumes: `SeriesService.delete(user_id, series_id, *, target_task_id=None)` from Task 1.
- Produces: unchanged public signature `TaskService.delete(self, user_id: int, task_id: int) -> None` with the new active-series behavior.

- [ ] **Step 1: Replace the completed-instance deletion test**

Replace `test_delete_completed_series_task_deletes_only_that_task` with:

```python
async def test_delete_completed_series_task_deletes_series_and_future_instances() -> None:
    """Deleting a completed instance also removes its series and future instances."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    completed = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    future = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=2))
    )
    past = repository.insert_task(
        make_task(task_id=3, series_id=series.id, due_at=fixed_now() - timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, completed.id)

    assert completed.deleted_at == fixed_now()
    assert series.deleted_at == fixed_now()
    assert future.id not in repository.tasks
    assert past.deleted_at is None
```

- [ ] **Step 2: Replace the overdue-instance deletion test**

Replace `test_delete_overdue_series_task_deletes_only_that_task` with:

```python
async def test_delete_overdue_series_task_deletes_series_and_future_instances() -> None:
    """Deleting an overdue instance removes the series and all future instances."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    overdue = repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=fixed_now() - timedelta(hours=1))
    )
    future = repository.insert_task(
        make_task(task_id=2, series_id=series.id, due_at=fixed_now() + timedelta(days=1))
    )
    service = make_service(repository)

    await service.delete(USER_ID, overdue.id)

    assert overdue.deleted_at == fixed_now()
    assert series.deleted_at == fixed_now()
    assert future.id not in repository.tasks
```

- [ ] **Step 3: Update the future-instance deletion assertion**

In `test_delete_series_task_deletes_the_series_and_future_instances`, replace:

```python
assert future.deleted_at == fixed_now()
```

with:

```python
assert future.id not in repository.tasks
```

Also update `test_delete_active_future_series_task_still_deletes_series_and_all_future_instances`
(`tests/test_task_management_tasks.py:1727-1756`):

```python
assert series.deleted_at == fixed_now()
assert earlier_future.id not in repository.tasks
assert target.id not in repository.tasks
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py::test_delete_completed_series_task_deletes_series_and_future_instances tests/test_task_management_tasks.py::test_delete_overdue_series_task_deletes_series_and_future_instances tests/test_task_management_tasks.py::test_delete_active_future_series_task_still_deletes_series_and_all_future_instances -v`

Expected: FAIL because completed and overdue tasks currently delete only themselves, and future instances are soft-deleted instead of physically removed.

- [ ] **Step 5: Implement the delegation**

Replace the conditional body of `TaskService.delete` with:

```python
task, series = await self._lock_update_target(user_id, task_id)
if series is not None:
    await self._series_service.delete(
        user_id,
        series.id,
        target_task_id=task.id,
    )
    return
now = self._clock()
await self._repository.cancel_task_reminders(user_id, task.id)
await self._repository.soft_delete_task(task, now, uuid4())
await self._repository.flush()
```

Update the docstring matrix: any task of an active series deletes the series; future incomplete instances are physically deleted; the selected completed/overdue instance is soft-deleted; one-off and soft-deleted-series tasks keep individual deletion.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_tasks.py -q`

Expected: PASS.

- [ ] **Step 7: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/tasks.py tests/test_task_management_tasks.py
git commit -m "feat: delete series when any series task is deleted"
```

---

### Task 3: Reject reopening past-dated tasks outside an active series

**Files:**
- Modify: `src/backend/task_management/tasks.py:31-39`, `src/backend/task_management/tasks.py:738-770`
- Test: `tests/test_task_management_tasks.py:377-410`

**Interfaces:**
- Consumes: `TaskService._lock_update_target`, `TaskService._user_timezone`, `TaskService._clock`.
- Produces: `TaskService.uncomplete` raises `BadRequestError` with message `Task due date is in the past` and the exact Russian description for completed tasks without an active series.

- [ ] **Step 1: Add the failing tests**

Append near the existing uncomplete tests:

```python
async def test_uncomplete_rejects_past_local_due_date_for_one_off_task() -> None:
    """A past-dated one-off task cannot be reopened without changing its date."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            due_at=fixed_now() - timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.uncomplete(USER_ID, task.id)

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == "Task due date is in the past"
    assert error.value.description == (
        "Нельзя вернуть в работу задачу с прошедшей датой. "
        "Сначала измените дату задачи."
    )
    assert task.completed_at == fixed_now()


async def test_uncomplete_rejects_past_date_for_task_of_soft_deleted_series() -> None:
    """A past-dated task of a soft-deleted series is rejected before recreation."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, deleted_at=fixed_now(), time_of_day=time(9, 0))
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)
    series_count = len(repository.series)

    with pytest.raises(BadRequestError):
        await service.uncomplete(USER_ID, task.id)

    assert task.completed_at == fixed_now()
    assert len(repository.series) == series_count


async def test_uncomplete_allows_past_date_for_active_series_task() -> None:
    """An active-series instance may be reopened even when its date is past."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.uncomplete(USER_ID, task.id)

    assert detail.task.completed_at is None
    assert task.series_id == series.id
    assert series.deleted_at is None


async def test_uncomplete_compares_local_dates_not_utc_dates() -> None:
    """A task due yesterday in UTC may still be today in the user's timezone."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Australia/Sydney"
    task = repository.insert_task(
        make_task(
            task_id=1,
            due_at=datetime(2026, 11, 3, 23, 0, tzinfo=UTC),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.uncomplete(USER_ID, task.id)

    assert detail.task.completed_at is None


async def test_uncomplete_past_date_is_allowed_for_already_active_task() -> None:
    """The past-date guard applies only when the task was actually completed."""
    repository = FakeTaskManagementRepository()
    task = repository.insert_task(
        make_task(
            task_id=1,
            due_at=fixed_now() - timedelta(days=1),
            completed_at=None,
        )
    )
    service = make_service(repository)

    detail = await service.uncomplete(USER_ID, task.id)

    assert detail.task.completed_at is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py::test_uncomplete_rejects_past_local_due_date_for_one_off_task tests/test_task_management_tasks.py::test_uncomplete_rejects_past_date_for_task_of_soft_deleted_series -v`

Expected: FAIL because `uncomplete` currently accepts both tasks.

- [ ] **Step 3: Add the error constants and guard**

Add next to the other module constants:

```python
_PAST_DUE_DATE = "Task due date is in the past"
_PAST_DUE_DATE_DESCRIPTION = (
    "Нельзя вернуть в работу задачу с прошедшей датой. "
    "Сначала измените дату задачи."
)
```

In `TaskService.uncomplete`, after locking the task and reading `now`:

```python
was_completed = task.completed_at is not None
if was_completed and series is None:
    zone = await self._user_timezone(user_id)
    if task.due_at.astimezone(zone).date() < now.astimezone(zone).date():
        raise BadRequestError(
            _PAST_DUE_DATE,
            description=_PAST_DUE_DATE_DESCRIPTION,
        )
```

Keep the existing `completed_at = None` and reminder-slot rebuild logic after the guard. Update the docstring with the `BadRequestError` and the local-date rule.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_tasks.py -q`

Expected: PASS.

- [ ] **Step 5: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/tasks.py tests/test_task_management_tasks.py
git commit -m "feat: reject reopening past-dated non-series tasks"
```

---

### Task 4: Recreate a deleted series and preserve its historical link

**Files:**
- Modify: `src/backend/task_management/ports.py` (near `get_series_tasks_for_update`, around line 1407)
- Modify: `src/backend/task_management/repository.py` (near `get_series_tasks_for_update`, around line 2108)
- Modify: `src/backend/task_management/tasks.py` (`_lock_update_target`, `uncomplete`, `_update_detached_task`)
- Modify: `tests/task_management_fakes.py` (near `get_series_tasks_for_update`, around line 940)
- Test: `tests/test_task_management_repository.py:1150-1200`, `tests/test_task_management_tasks.py:1293-1395`, `tests/test_task_management_tasks.py:377-410`

**Interfaces:**
- Consumes: `SeriesService.create_from_task(user_id, task, *, frequency: SeriesFrequency, interval_days: int | None = None) -> TaskSeries`, `TaskManagementRepository.get_task`, `get_series`.
- Produces:
  - `TaskManagementRepository.series_due_slot_exists(self, series_id: int, user_id: int, due_at: datetime, *, exclude_task_id: int | None = None) -> bool`
  - `TaskService._lock_update_target(self, user_id: int, task_id: int, *, include_deleted_series: bool = False) -> tuple[Task, TaskSeries | None]`
  - `TaskService.uncomplete` recreates a soft-deleted series for a completed task.

- [ ] **Step 1: Write the repository test**

Append near the series repository tests:

```python
async def test_series_due_slot_exists_is_owner_scoped_and_excludes_task() -> None:
    """The slot lookup is owner-scoped and ignores the edited task."""
    session = RecordingSession(results=[RecordingResult(scalar=1)])
    repository = make_repository(session)

    exists = await repository.series_due_slot_exists(
        3,
        USER_ID,
        NOW,
        exclude_task_id=9,
    )

    assert exists is True
    sql = compile_statement(session.statements[-1])
    assert "tasks.series_id = 3" in sql
    assert "tasks.user_id = 7" in sql
    assert "tasks.deleted_at IS NULL" in sql
    assert "tasks.due_at = " in sql
    assert "tasks.id != 9" in sql
    assert "LIMIT 1" in sql
```

- [ ] **Step 2: Run the repository test to verify it fails**

Run: `poetry run pytest tests/test_task_management_repository.py::test_series_due_slot_exists_is_owner_scoped_and_excludes_task -v`

Expected: FAIL with `AttributeError: 'SqlAlchemyTaskManagementRepository' object has no attribute 'series_due_slot_exists'`.

- [ ] **Step 3: Implement the port, repository method, and fake**

Add to `TaskManagementRepository`:

```python
async def series_due_slot_exists(
    self,
    series_id: int,
    user_id: int,
    due_at: datetime,
    *,
    exclude_task_id: int | None = None,
) -> bool:
    """
    Check whether a series already owns a non-deleted task at a due instant.

    Purpose:
        Preserve the historical series link on completed-instance edits while
        avoiding a unique ``(series_id, due_at)`` slot collision.

    Args:
        series_id: Series identifier.
        user_id: Owner identifier.
        due_at: Candidate timezone-aware due instant.
        exclude_task_id: Task to ignore during the comparison, usually the
            task being edited.

    Returns:
        bool: ``True`` when another non-deleted task of the series occupies
            the slot.

    Notes:
        - Includes completed tasks because the partial unique index covers
          every non-deleted row.
        - Performs no writes and does not commit.
    """
    ...
```

Implement in `SqlAlchemyTaskManagementRepository`:

```python
async def series_due_slot_exists(
    self,
    series_id: int,
    user_id: int,
    due_at: datetime,
    *,
    exclude_task_id: int | None = None,
) -> bool:
    """Check whether another non-deleted task occupies a series due slot."""
    statement = select(Task.id).where(
        Task.series_id == series_id,
        Task.user_id == user_id,
        Task.deleted_at.is_(None),
        Task.due_at == due_at,
    )
    if exclude_task_id is not None:
        statement = statement.where(Task.id != exclude_task_id)
    result = await self._session.execute(statement.limit(1))
    return result.scalar_one_or_none() is not None
```

Implement in `FakeTaskManagementRepository`:

```python
async def series_due_slot_exists(
    self,
    series_id: int,
    user_id: int,
    due_at: datetime,
    *,
    exclude_task_id: int | None = None,
) -> bool:
    """Return whether another non-deleted task occupies a series due slot."""
    return any(
        task.series_id == series_id
        and task.user_id == user_id
        and task.deleted_at is None
        and task.due_at == due_at
        and task.id != exclude_task_id
        for task in self.tasks.values()
    )
```

- [ ] **Step 4: Run the repository test to verify it passes**

Run: `poetry run pytest tests/test_task_management_repository.py::test_series_due_slot_exists_is_owner_scoped_and_excludes_task -v`

Expected: PASS.

- [ ] **Step 5: Add the failing service tests**

Append near the uncomplete tests:

```python
@pytest.mark.parametrize(
    ("frequency", "interval_days", "expected"),
    [
        ("daily", 1, TaskRecurrence.REGULAR),
        ("periodic", 3, TaskRecurrence.PERIODIC),
    ],
)
async def test_uncomplete_recreates_deleted_series_from_saved_schedule(
    frequency: str,
    interval_days: int,
    expected: TaskRecurrence,
) -> None:
    """Reopening a completed task restores its deleted series schedule."""
    repository = FakeTaskManagementRepository()
    old_series = repository.insert_series(
        make_series(
            series_id=1,
            frequency=frequency,
            interval_days=interval_days,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=old_series.id,
            due_at=fixed_now() + timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.uncomplete(USER_ID, task.id)

    assert task.completed_at is None
    assert task.series_id is not None
    assert task.series_id != old_series.id
    new_series = repository.series[task.series_id]
    assert new_series.deleted_at is None
    assert new_series.frequency == frequency
    assert new_series.interval_days == interval_days
    assert detail.recurrence is expected
    assert any(
        item.series_id == new_series.id and item.id != task.id
        for item in repository.tasks.values()
    )
    assert old_series.deleted_at == fixed_now()


async def test_uncomplete_does_not_recreate_active_series() -> None:
    """An active series is reused instead of creating a second one."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(series_id=1, time_of_day=time(9, 0), starts_on=date(2026, 11, 1))
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=fixed_now() - timedelta(days=1),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)
    series_count = len(repository.series)

    await service.uncomplete(USER_ID, task.id)

    assert task.series_id == series.id
    assert len(repository.series) == series_count
    assert series.deleted_at is None


async def test_update_due_at_on_completed_soft_deleted_series_task_keeps_link() -> None:
    """A completed task keeps its historical series link on a free due slot."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    task = repository.insert_task(
        make_task(
            task_id=1,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.update(
        USER_ID,
        task.id,
        due_at=datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
    )

    assert task.series_id == series.id
    assert detail.recurrence is TaskRecurrence.REGULAR


async def test_update_due_at_on_completed_soft_deleted_series_task_clears_link_on_collision() -> None:
    """A completed task drops its historical link when the new slot is occupied."""
    repository = FakeTaskManagementRepository()
    series = repository.insert_series(
        make_series(
            series_id=1,
            deleted_at=fixed_now(),
            time_of_day=time(9, 0),
            starts_on=date(2026, 11, 1),
        )
    )
    target_due_at = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    repository.insert_task(
        make_task(task_id=1, series_id=series.id, due_at=target_due_at)
    )
    task = repository.insert_task(
        make_task(
            task_id=2,
            series_id=series.id,
            due_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
            completed_at=fixed_now(),
        )
    )
    service = make_service(repository)

    detail = await service.update(USER_ID, task.id, due_at=target_due_at)

    assert task.series_id is None
    assert detail.recurrence is TaskRecurrence.ONE_OFF
```

- [ ] **Step 6: Run the service tests to verify they fail**

Run: `poetry run pytest tests/test_task_management_tasks.py::test_uncomplete_recreates_deleted_series_from_saved_schedule tests/test_task_management_tasks.py::test_update_due_at_on_completed_soft_deleted_series_task_keeps_link -v`

Expected: FAIL because `uncomplete` does not recreate and the due change clears `series_id` for every task.

- [ ] **Step 7: Implement the deleted-series lookup and recreation**

Add `include_deleted_series: bool = False` to `_lock_update_target`. In the series lookup block:

```python
if series.deleted_at is not None and not include_deleted_series:
    series = None
```

In `TaskService.uncomplete`, call:

```python
task, series = await self._lock_update_target(
    user_id,
    task_id,
    include_deleted_series=True,
)
```

Change the guard condition from Task 3 to:

```python
was_completed = task.completed_at is not None
if was_completed and (series is None or series.deleted_at is not None):
    zone = await self._user_timezone(user_id)
    if task.due_at.astimezone(zone).date() < now.astimezone(zone).date():
        raise BadRequestError(
            _PAST_DUE_DATE,
            description=_PAST_DUE_DATE_DESCRIPTION,
        )
```

After clearing `completed_at` and rebuilding reminder slots, add:

```python
if was_completed and series is not None and series.deleted_at is not None:
    await self._series_service.create_from_task(
        user_id,
        task,
        frequency=SeriesFrequency(series.frequency),
        interval_days=series.interval_days,
    )
```

- [ ] **Step 8: Preserve the historical link in `_update_detached_task`**

Replace the existing stale-link clearing block:

```python
if task.series_id is not None and due_at is not None and due_at != task.due_at:
    task.series_id = None
```

with:

```python
if (
    recurrence is None
    and task.series_id is not None
    and due_at is not None
    and due_at != task.due_at
):
    if task.completed_at is None or await self._repository.series_due_slot_exists(
        task.series_id,
        user_id,
        due_at,
        exclude_task_id=task.id,
    ):
        task.series_id = None
```

Update the docstrings of `_lock_update_target`, `uncomplete`, and `_update_detached_task` to describe the deleted-series lookup, recreation, and link-preservation rule.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `poetry run pytest tests/test_task_management_tasks.py tests/test_task_management_repository.py -q`

Expected: PASS.

- [ ] **Step 10: Run project checks and commit**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
git add src/backend/task_management/ports.py src/backend/task_management/repository.py src/backend/task_management/tasks.py tests/task_management_fakes.py tests/test_task_management_tasks.py tests/test_task_management_repository.py
git commit -m "feat: restore recurring series when reopening deleted-series tasks"
```

---

### Task 5: Update API documentation and run final verification

**Files:**
- Modify: `src/backend/api/routes/tasks.py:451-523`
- Modify: `src/backend/api/routes/task_series.py:267-300`
- Modify: `README.md:317`, `README.md:376-395`
- Modify: `docs/api.md:1286-1330`, `docs/api.md:1430-1440`

**Interfaces:**
- Consumes: the behavior implemented in Tasks 1-4.
- Produces: user-facing documentation consistent with the new contract.

- [ ] **Step 1: Update route docstrings**

In `uncomplete_task`:

- add `BadRequestError (HTTP 400): If the task was completed, has no active series, and its local due date is in the past.`
- note that active-series instances reopen without the date guard;
- note that a completed task of a soft-deleted series recreates the series with the saved `frequency`/`interval_days`.

In `delete_task`:

- state that any task of an active series deletes the series;
- state that future incomplete instances are physically deleted;
- state that the selected completed/overdue instance is soft-deleted and history is preserved.

In `delete_series` (`task_series.py`):

- state that future incomplete instances are physically deleted.

- [ ] **Step 2: Update `README.md`**

Replace the `DELETE /api/v1/tasks/{task_id}` matrix with:

```markdown
| Состояние задачи | Поведение |
| --- | --- |
| Разовая задача или задача soft-deleted серии | soft-delete только задачи, отменить её доставки |
| Любая задача активной серии | soft-delete серии; hard delete всех будущих незавершённых экземпляров; выбранная завершённая или просроченная задача soft-delete; остальная история сохраняется |
```

Add to the task behavior bullets:

```markdown
- `uncomplete` разовой задачи или задачи soft-deleted серии с прошедшей
  локальной датой возвращает `400 bad_request` и требует изменить дату.
  Экземпляр активной серии возвращается в работу без этого ограничения.
- Если у завершённой задачи soft-deleted серия, успешный `uncomplete`
  создаёт серию заново с прежними `frequency` и `interval_days`; задача
  становится первым экземпляром новой серии.
```

Update the recurring-series section: deleting a series physically removes
future incomplete instances while preserving completed and past history.

- [ ] **Step 3: Update `docs/api.md`**

In `9.7 POST /api/v1/tasks/{task_id}/uncomplete`:

- document the `400 bad_request` past-date case with the exact description;
- document that it applies to one-off and soft-deleted-series tasks only;
- document series recreation with saved `frequency`/`interval_days`.

In `9.8 DELETE /api/v1/tasks/{task_id}`:

- replace the matrix with the new active-series rule;
- state that future incomplete instances are hard-deleted;
- replace the paragraph at `docs/api.md:1321-1323` with a note that deleting
  any task of an active series deletes the whole series, including when the
  selected instance is completed or overdue; the API has no per-instance
  deletion for active series.

In the task-series delete section:

- state that future incomplete instances are hard-deleted;
- keep the note that completed and past history remains.

- [ ] **Step 4: Run the full verification suite**

```bash
poetry run pytest -q
poetry run ruff check .
poetry run ruff format --check src tests
poetry run mypy src tests
graphify update .
```

Expected: all commands exit with status 0.

- [ ] **Step 5: Inspect the working tree and commit only intended files**

```bash
git status --short
git add src/backend/api/routes/tasks.py src/backend/api/routes/task_series.py README.md docs/api.md
git commit -m "docs: document recurring task deletion and reopening"
```

Do not stage the pre-existing deleted plan files or unrelated admin changes.

---

## Final Verification

- [ ] `poetry run pytest -q`
- [ ] `poetry run ruff check .`
- [ ] `poetry run ruff format --check src tests`
- [ ] `poetry run mypy src tests`
- [ ] `graphify update .`
- [ ] `git status --short` shows no unintended staged files.
- [ ] The spec at `docs/superpowers/specs/2026-11-09-recurring-task-deletion-reactivation-design.md` matches the implemented behavior.
