from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from backend.core.errors import BadRequestError, ConflictError, ErrorCode, NotFoundError
from backend.models.color_palette import ColorPalette
from backend.models.label import Label
from backend.task_management.labels import LabelService
from backend.task_management.palette import PaletteService
from backend.task_management.ports import TaskManagementRepository

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
USER_ID = 7


def clock() -> datetime:
    """Return the fixed service clock."""
    return NOW


def make_repository() -> AsyncMock:
    """Build an async repository double constrained to the task-management port."""
    return AsyncMock(spec=TaskManagementRepository)


def make_color(*, color_id: int = 1) -> ColorPalette:
    """Build a palette color with deterministic values."""
    return ColorPalette(
        id=color_id,
        slug=f"color-{color_id}",
        name=f"Color {color_id}",
        hex="#000000",
        sort_order=color_id,
        is_active=True,
    )


def make_label(*, label_id: int = 1, name: str = "Home", color_id: int = 1) -> Label:
    """Build a user label with deterministic values."""
    return Label(
        id=label_id,
        user_id=USER_ID,
        name=name,
        color_id=color_id,
        deleted_at=None,
    )


def make_service(repository: AsyncMock) -> LabelService:
    """Build a label service with the fixed clock and real palette rules."""
    return LabelService(repository, PaletteService(repository), clock=clock)


async def _return_label(label: Label) -> Label:
    """Return the label passed to the repository double."""
    return label


async def test_list_returns_user_labels() -> None:
    """Label listing delegates to the repository for the current user."""
    repository = make_repository()
    labels = [make_label(label_id=1), make_label(label_id=2, name="Work")]
    repository.list_labels.return_value = labels

    service = make_service(repository)

    result = await service.list(USER_ID)

    assert list(result) == labels
    repository.list_labels.assert_awaited_once_with(USER_ID)


async def test_get_returns_label() -> None:
    """An owned label is returned by id."""
    repository = make_repository()
    label = make_label()
    repository.get_label.return_value = label

    service = make_service(repository)

    result = await service.get(USER_ID, label.id)

    assert result is label
    repository.get_label.assert_awaited_once_with(USER_ID, label.id)


async def test_get_cross_user_label_raises_not_found() -> None:
    """Another user's label is indistinguishable from a missing label."""
    repository = make_repository()
    repository.get_label.return_value = None

    service = make_service(repository)

    with pytest.raises(NotFoundError) as error:
        await service.get(USER_ID, 999)

    assert error.value.code is ErrorCode.NOT_FOUND


async def test_create_trims_name_and_uses_default_color() -> None:
    """Label creation strips the name and resolves the default active color."""
    repository = make_repository()
    color = make_color()
    repository.label_name_exists.return_value = False
    repository.get_default_palette_color.return_value = color
    repository.add_label.side_effect = _return_label

    service = make_service(repository)

    result = await service.create(USER_ID, name="  Home  ", color_id=None)

    assert result.user_id == USER_ID
    assert result.name == "Home"
    assert result.color_id == color.id
    repository.label_name_exists.assert_awaited_once_with(USER_ID, "Home")
    repository.get_default_palette_color.assert_awaited_once_with()
    repository.add_label.assert_awaited_once_with(result)


async def test_create_rejects_empty_name() -> None:
    """A label name that is empty after trimming is rejected."""
    repository = make_repository()

    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.create(USER_ID, name="   ", color_id=None)

    assert error.value.code is ErrorCode.BAD_REQUEST
    repository.add_label.assert_not_awaited()


async def test_create_rejects_duplicate_name() -> None:
    """A duplicate active label name raises a conflict."""
    repository = make_repository()
    repository.label_name_exists.return_value = True

    service = make_service(repository)

    with pytest.raises(ConflictError) as error:
        await service.create(USER_ID, name="Home", color_id=None)

    assert str(error.value) == "Label name already exists"
    repository.add_label.assert_not_awaited()


async def test_update_changes_trimmed_name_and_color() -> None:
    """Label updates trim names and validate the new active color."""
    repository = make_repository()
    label = make_label()
    new_color = make_color(color_id=2)
    repository.get_label.return_value = label
    repository.label_name_exists.return_value = False
    repository.get_palette_color.return_value = new_color

    service = make_service(repository)

    result = await service.update(
        USER_ID,
        label.id,
        name="  Errands  ",
        color_id=new_color.id,
    )

    assert result is label
    assert label.name == "Errands"
    assert label.color_id == new_color.id
    repository.label_name_exists.assert_awaited_once_with(
        USER_ID,
        "Errands",
        exclude_label_id=label.id,
    )
    repository.get_palette_color.assert_awaited_once_with(new_color.id)


async def test_update_keeps_omitted_fields() -> None:
    """Omitted label fields keep their current values."""
    repository = make_repository()
    label = make_label(name="Keep", color_id=3)
    repository.get_label.return_value = label

    service = make_service(repository)

    await service.update(USER_ID, label.id)

    assert label.name == "Keep"
    assert label.color_id == 3


async def test_update_rejects_empty_name() -> None:
    """An update name that is empty after trimming is rejected."""
    repository = make_repository()
    label = make_label()
    repository.get_label.return_value = label

    service = make_service(repository)

    with pytest.raises(BadRequestError):
        await service.update(USER_ID, label.id, name="  ")


async def test_update_rejects_duplicate_name() -> None:
    """An update cannot take a name used by another active label."""
    repository = make_repository()
    label = make_label()
    repository.get_label.return_value = label
    repository.label_name_exists.return_value = True

    service = make_service(repository)

    with pytest.raises(ConflictError) as error:
        await service.update(USER_ID, label.id, name="Other")

    assert str(error.value) == "Label name already exists"


async def test_update_cannot_move_to_inactive_color() -> None:
    """Moving a label to an inactive palette color is rejected."""
    repository = make_repository()
    label = make_label(color_id=1)
    repository.get_label.return_value = label
    repository.get_palette_color.return_value = ColorPalette(
        id=9,
        slug="inactive",
        name="Inactive",
        hex="#000000",
        sort_order=9,
        is_active=False,
    )

    service = make_service(repository)

    with pytest.raises(BadRequestError) as error:
        await service.update(USER_ID, label.id, color_id=9)

    assert error.value.code is ErrorCode.BAD_REQUEST
    assert label.color_id == 1


async def test_delete_soft_deletes_with_injected_time() -> None:
    """Deleting a label records the injected current time."""
    repository = make_repository()
    label = make_label()
    repository.get_label.return_value = label

    service = make_service(repository)

    await service.delete(USER_ID, label.id)

    repository.get_label.assert_awaited_once_with(USER_ID, label.id, for_update=True)
    repository.soft_delete_label.assert_awaited_once_with(label, NOW)


async def test_delete_is_idempotent() -> None:
    """Deleting an already deleted label succeeds without another write."""
    repository = make_repository()
    label = make_label()
    repository.get_label.side_effect = [label, None]

    service = make_service(repository)

    await service.delete(USER_ID, label.id)
    await service.delete(USER_ID, label.id)

    repository.soft_delete_label.assert_awaited_once_with(label, NOW)


async def test_delete_cross_user_label_is_idempotent() -> None:
    """Another user's label is treated as already absent on delete."""
    repository = make_repository()
    repository.get_label.return_value = None

    service = make_service(repository)

    await service.delete(USER_ID, 999)

    repository.soft_delete_label.assert_not_awaited()
