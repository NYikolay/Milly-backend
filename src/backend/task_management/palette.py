from __future__ import annotations

from collections.abc import Sequence

from backend.core.errors import BadRequestError, ConflictError, NotFoundError
from backend.models.color_palette import ColorPalette
from backend.task_management.ports import TaskManagementRepository

_COLOR_UNAVAILABLE = "Color is not available"
_COLOR_NOT_FOUND = "Palette color not found"
_LAST_ACTIVE_COLOR = "At least one active color is required"


class PaletteService:
    """Resolve palette colors individually or in batches and protect the last active color."""

    def __init__(self, repository: TaskManagementRepository) -> None:
        """
        Initialize the service with its persistence port.

        Purpose:
            Resolve palette colors without depending on SQLAlchemy.

        Args:
            repository: Persistence operations for the global palette.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository

    async def list_active(self) -> Sequence[ColorPalette]:
        """
        List selectable palette colors.

        Purpose:
            Return only active colors in deterministic display order.

        Returns:
            Sequence[ColorPalette]: Active colors ordered by
                ``(sort_order, id)``.

        Notes:
            - Sorts defensively even though the repository already orders the
              rows; inactive rows are never exposed.
        """
        colors = await self._repository.list_active_palette_colors()
        return sorted(
            (color for color in colors if color.is_active),
            key=lambda color: (color.sort_order, color.id),
        )

    async def get_color(self, color_id: int) -> ColorPalette:
        """
        Load a palette color for display.

        Purpose:
            Return the stored color of an existing project or label even when
            the palette entry is no longer active.

        Args:
            color_id: Palette color identifier.

        Returns:
            ColorPalette: The matching color regardless of its active state.

        Raises:
            NotFoundError: If the color does not exist.

        Notes:
            - Performs no writes and does not commit.
        """
        color = await self._repository.get_palette_color(color_id)
        if color is None:
            raise NotFoundError(_COLOR_NOT_FOUND)
        return color

    async def get_colors(self, color_ids: Sequence[int]) -> dict[int, ColorPalette]:
        """
        Load multiple palette colors for display.

        Purpose:
            Resolve the stored colors of a project page in one repository
            query while preserving the not-found contract of single lookups.

        Args:
            color_ids: Palette color identifiers to load.

        Returns:
            dict[int, ColorPalette]: Matching colors keyed by identifier,
                including colors that are no longer active.

        Raises:
            NotFoundError: If any requested color does not exist.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        colors = await self._repository.get_palette_colors_by_ids(color_ids)
        if any(color_id not in colors for color_id in color_ids):
            raise NotFoundError(_COLOR_NOT_FOUND)
        return colors

    async def resolve_for_write(self, color_id: int | None) -> ColorPalette:
        """
        Resolve the palette color for a new project or label.

        Purpose:
            Apply the default active color when no selection is provided and
            reject missing or inactive selections.

        Args:
            color_id: Requested palette color identifier, or ``None``.

        Returns:
            ColorPalette: The selected or default active color.

        Raises:
            BadRequestError: If the requested color is missing or inactive, or
                no default active color exists.

        Notes:
            - Performs no writes and does not commit.
        """
        if color_id is None:
            color = await self._repository.get_default_palette_color()
            if color is None:
                raise BadRequestError(_COLOR_UNAVAILABLE)
            return color
        return await self.ensure_active(color_id)

    async def ensure_active(self, color_id: int) -> ColorPalette:
        """
        Load an explicit color that must be active.

        Purpose:
            Validate a color change on an existing project or label.

        Args:
            color_id: Requested palette color identifier.

        Returns:
            ColorPalette: The matching active color.

        Raises:
            BadRequestError: If the color is missing or inactive.

        Notes:
            - Performs no writes and does not commit.
        """
        color = await self._repository.get_palette_color(color_id)
        if color is None or not color.is_active:
            raise BadRequestError(_COLOR_UNAVAILABLE)
        return color

    async def ensure_can_deactivate(self, color_id: int) -> None:
        """
        Ensure deactivating a color leaves another active color.

        Purpose:
            Protect the invariant that clients can always select a color.

        Args:
            color_id: Palette color about to be deactivated.

        Returns:
            None: The operation is allowed.

        Raises:
            ConflictError: If no other active color remains.

        Notes:
            - Deactivating an already inactive or missing color is a no-op for
              the active count and therefore allowed.
            - Performs no writes and does not commit.
        """
        await self._ensure_other_active_color(color_id)

    async def ensure_can_delete(self, color_id: int) -> None:
        """
        Ensure deleting a color leaves another active color.

        Purpose:
            Protect the invariant that clients can always select a color.

        Args:
            color_id: Palette color about to be deleted.

        Returns:
            None: The operation is allowed.

        Raises:
            ConflictError: If no other active color remains.

        Notes:
            - Deleting an already inactive or missing color is a no-op for the
              active count and therefore allowed.
            - Performs no writes and does not commit.
        """
        await self._ensure_other_active_color(color_id)

    async def _ensure_other_active_color(self, color_id: int) -> None:
        """
        Reject an operation that would remove the last active color.

        Purpose:
            Share the active-color invariant between deactivation and deletion.

        Args:
            color_id: Palette color affected by the operation.

        Returns:
            None: At least one other active color remains.

        Raises:
            ConflictError: If the operation would leave zero active colors.

        Notes:
            - Performs no writes and does not commit.
        """
        color = await self._repository.get_palette_color(color_id)
        active_count = await self._repository.count_active_palette_colors()
        other_active = active_count - (1 if color is not None and color.is_active else 0)
        if other_active <= 0:
            raise ConflictError(_LAST_ACTIVE_COLOR)
