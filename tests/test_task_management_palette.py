from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.core.errors import BadRequestError, ConflictError, ErrorCode
from backend.models.color_palette import ColorPalette
from backend.task_management.palette import PaletteService
from backend.task_management.ports import TaskManagementRepository


def make_repository() -> AsyncMock:
    """Build an async repository double constrained to the task-management port."""
    return AsyncMock(spec=TaskManagementRepository)


def make_color(
    *,
    color_id: int,
    sort_order: int,
    is_active: bool = True,
) -> ColorPalette:
    """Build a palette color with deterministic values."""
    return ColorPalette(
        id=color_id,
        slug=f"color-{color_id}",
        name=f"Color {color_id}",
        hex="#000000",
        sort_order=sort_order,
        is_active=is_active,
    )


async def test_list_active_returns_only_active_colors_sorted() -> None:
    """The palette exposes only active colors ordered by sort order and id."""
    repository = make_repository()
    second = make_color(color_id=2, sort_order=2)
    first = make_color(color_id=1, sort_order=1)
    inactive = make_color(color_id=3, sort_order=0, is_active=False)
    repository.list_active_palette_colors.return_value = [second, inactive, first]

    service = PaletteService(repository)

    result = await service.list_active()

    assert list(result) == [first, second]
    repository.list_active_palette_colors.assert_awaited_once_with()


async def test_resolve_for_write_defaults_to_first_active_color() -> None:
    """An omitted color id resolves to the repository default active color."""
    repository = make_repository()
    default = make_color(color_id=1, sort_order=1)
    repository.get_default_palette_color.return_value = default

    service = PaletteService(repository)

    result = await service.resolve_for_write(None)

    assert result is default
    repository.get_default_palette_color.assert_awaited_once_with()
    repository.get_palette_color.assert_not_awaited()


async def test_resolve_for_write_rejects_missing_default_color() -> None:
    """A missing default active color is reported as a bad request."""
    repository = make_repository()
    repository.get_default_palette_color.return_value = None

    service = PaletteService(repository)

    with pytest.raises(BadRequestError) as error:
        await service.resolve_for_write(None)

    assert error.value.code is ErrorCode.BAD_REQUEST


async def test_resolve_for_write_rejects_missing_color() -> None:
    """An unknown explicit color id is reported as a bad request."""
    repository = make_repository()
    repository.get_palette_color.return_value = None

    service = PaletteService(repository)

    with pytest.raises(BadRequestError) as error:
        await service.resolve_for_write(99)

    assert error.value.code is ErrorCode.BAD_REQUEST


async def test_resolve_for_write_rejects_inactive_color() -> None:
    """An inactive explicit color id is reported as a bad request."""
    repository = make_repository()
    repository.get_palette_color.return_value = make_color(
        color_id=9,
        sort_order=1,
        is_active=False,
    )

    service = PaletteService(repository)

    with pytest.raises(BadRequestError) as error:
        await service.resolve_for_write(9)

    assert error.value.code is ErrorCode.BAD_REQUEST


async def test_ensure_active_returns_active_color() -> None:
    """An active explicit color id is returned unchanged."""
    repository = make_repository()
    color = make_color(color_id=4, sort_order=4)
    repository.get_palette_color.return_value = color

    service = PaletteService(repository)

    result = await service.ensure_active(4)

    assert result is color
    repository.get_palette_color.assert_awaited_once_with(4)


async def test_ensure_active_rejects_inactive_color() -> None:
    """An inactive explicit color cannot be used for a write."""
    repository = make_repository()
    repository.get_palette_color.return_value = make_color(
        color_id=4,
        sort_order=4,
        is_active=False,
    )

    service = PaletteService(repository)

    with pytest.raises(BadRequestError):
        await service.ensure_active(4)


async def test_ensure_can_deactivate_rejects_last_active_color() -> None:
    """Deactivating the last active color raises a conflict."""
    repository = make_repository()
    repository.get_palette_color.return_value = make_color(color_id=1, sort_order=1)
    repository.count_active_palette_colors.return_value = 1

    service = PaletteService(repository)

    with pytest.raises(ConflictError) as error:
        await service.ensure_can_deactivate(1)

    assert error.value.code is ErrorCode.CONFLICT


async def test_ensure_can_delete_rejects_last_active_color() -> None:
    """Deleting the last active color raises a conflict."""
    repository = make_repository()
    repository.get_palette_color.return_value = make_color(color_id=1, sort_order=1)
    repository.count_active_palette_colors.return_value = 1

    service = PaletteService(repository)

    with pytest.raises(ConflictError) as error:
        await service.ensure_can_delete(1)

    assert error.value.code is ErrorCode.CONFLICT


async def test_ensure_can_deactivate_allows_another_active_color() -> None:
    """Deactivating a color is allowed while another active color remains."""
    repository = make_repository()
    repository.get_palette_color.return_value = make_color(color_id=1, sort_order=1)
    repository.count_active_palette_colors.return_value = 2

    service = PaletteService(repository)

    await service.ensure_can_deactivate(1)


async def test_ensure_can_delete_allows_inactive_color_with_active_peer() -> None:
    """Deleting an inactive color does not reduce the active color count."""
    repository = make_repository()
    repository.get_palette_color.return_value = make_color(
        color_id=3,
        sort_order=3,
        is_active=False,
    )
    repository.count_active_palette_colors.return_value = 1

    service = PaletteService(repository)

    await service.ensure_can_delete(3)
