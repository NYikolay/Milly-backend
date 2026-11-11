from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from backend.application.task_management.task_references import TaskReferences
from backend.core.errors import BadRequestError, ErrorCode, NotFoundError
from tests.task_management_fakes import (
    FakeTaskManagementRepository,
    fixed_now,
    make_label,
    make_project,
)

USER_ID = 7


def make_references(repository: FakeTaskManagementRepository) -> TaskReferences:
    """Build task references over the fake repository ports."""
    return TaskReferences(repository, repository, repository)


async def test_validate_project_rejects_missing_project() -> None:
    """A missing or cross-user project is reported as not found."""
    repository = FakeTaskManagementRepository()
    references = make_references(repository)

    with pytest.raises(NotFoundError) as error:
        await references.validate_project(USER_ID, 999)

    assert error.value.code is ErrorCode.NOT_FOUND
    assert error.value.message == "Project not found"


async def test_validate_project_rejects_archived_project() -> None:
    """An archived project cannot receive new task references."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project(archived_at=fixed_now()))
    references = make_references(repository)

    with pytest.raises(BadRequestError) as error:
        await references.validate_project(USER_ID, project.id)

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == "Project is archived"


async def test_validate_project_accepts_absent_and_active_projects() -> None:
    """An absent reference is allowed and an active project passes validation."""
    repository = FakeTaskManagementRepository()
    project = repository.insert_project(make_project())
    references = make_references(repository)

    await references.validate_project(USER_ID, None)
    await references.validate_project(USER_ID, project.id)


async def test_validate_labels_deduplicates_in_input_order() -> None:
    """Owned labels are returned once each, preserving the request order."""
    repository = FakeTaskManagementRepository()
    first = repository.insert_label(make_label(label_id=1, name="Home"))
    second = repository.insert_label(make_label(label_id=2, name="Work"))
    references = make_references(repository)

    result = await references.validate_labels(USER_ID, (second.id, first.id, second.id))

    assert result == (second.id, first.id)


async def test_validate_labels_rejects_unknown_label() -> None:
    """A label selection with an unknown identifier is rejected."""
    repository = FakeTaskManagementRepository()
    label = repository.insert_label(make_label(label_id=1))
    references = make_references(repository)

    with pytest.raises(NotFoundError) as error:
        await references.validate_labels(USER_ID, (label.id, 999))

    assert error.value.code is ErrorCode.NOT_FOUND
    assert error.value.message == "Label not found"


async def test_user_timezone_defaults_to_utc_when_unset() -> None:
    """An unset stored timezone resolves to UTC."""
    repository = FakeTaskManagementRepository()
    references = make_references(repository)

    zone = await references.user_timezone(USER_ID)

    assert zone == ZoneInfo("UTC")


async def test_user_timezone_rejects_invalid_stored_zone() -> None:
    """An invalid stored timezone raises a bad-request error."""
    repository = FakeTaskManagementRepository()
    repository.user_timezones[USER_ID] = "Not/AZone"
    references = make_references(repository)

    with pytest.raises(BadRequestError) as error:
        await references.user_timezone(USER_ID)

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == "Invalid timezone"


def test_named_timezone_rejects_invalid_name() -> None:
    """An invalid IANA name raises a bad-request error."""
    repository = FakeTaskManagementRepository()
    references = make_references(repository)

    with pytest.raises(BadRequestError) as error:
        references.named_timezone("Not/AZone")

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == "Invalid timezone"
