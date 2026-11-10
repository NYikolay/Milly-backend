"""SQLAlchemy implementation of the palette persistence port."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import ColorPalette
from backend.infrastructure.persistence.sqlalchemy.mappers import color_to_entity
from backend.models.color_palette import ColorPalette as ColorPaletteRow
from backend.models.label import Label as LabelRow
from backend.models.project import Project as ProjectRow


class SqlAlchemyPaletteRepository:
    """SQLAlchemy implementation of the palette persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the palette adapter with a request-scoped session.

        Purpose:
            Bind palette reads to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

    async def list_palette_colors(self) -> Sequence[ColorPalette]:
        """
        List every palette color in display order.

        Purpose:
            Expose the complete global palette for administrative consumers.

        Returns:
            Sequence[ColorPalette]: Domain colors ordered by
                ``(sort_order, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPaletteRow).order_by(ColorPaletteRow.sort_order, ColorPaletteRow.id)
        )
        return [color_to_entity(row) for row in result.scalars().all()]

    async def list_active_palette_colors(self) -> Sequence[ColorPalette]:
        """
        List active palette colors in display order.

        Purpose:
            Provide the selectable client palette.

        Returns:
            Sequence[ColorPalette]: Active domain colors ordered by
                ``(sort_order, id)``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPaletteRow)
            .where(ColorPaletteRow.is_active.is_(True))
            .order_by(ColorPaletteRow.sort_order, ColorPaletteRow.id)
        )
        return [color_to_entity(row) for row in result.scalars().all()]

    async def get_palette_color(self, color_id: int) -> ColorPalette | None:
        """
        Load one palette color by identifier.

        Purpose:
            Validate an explicit color selection and resolve display data.

        Args:
            color_id: Palette color identifier.

        Returns:
            ColorPalette | None: The matching domain color, or ``None``.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPaletteRow).where(ColorPaletteRow.id == color_id)
        )
        row = result.scalar_one_or_none()
        return color_to_entity(row) if row is not None else None

    async def get_palette_colors_by_ids(
        self,
        color_ids: Sequence[int],
    ) -> dict[int, ColorPalette]:
        """
        Load multiple palette colors by identifier.

        Purpose:
            Resolve project colors and label colors without one query per row.

        Args:
            color_ids: Palette color identifiers to load.

        Returns:
            dict[int, ColorPalette]: Matching domain colors keyed by
                identifier; unknown identifiers are absent.

        Notes:
            - Returns an empty mapping without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not color_ids:
            return {}
        result = await self._session.execute(
            select(ColorPaletteRow).where(ColorPaletteRow.id.in_(color_ids))
        )
        return {row.id: color_to_entity(row) for row in result.scalars().all()}

    async def get_default_palette_color(self) -> ColorPalette | None:
        """
        Load the first active palette color.

        Purpose:
            Resolve the color used when a project or label omits ``color_id``.

        Returns:
            ColorPalette | None: The first active domain color by
                ``(sort_order, id)``, or ``None`` when no color is active.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(ColorPaletteRow)
            .where(ColorPaletteRow.is_active.is_(True))
            .order_by(ColorPaletteRow.sort_order, ColorPaletteRow.id)
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return color_to_entity(row) if row is not None else None

    async def count_active_palette_colors(self) -> int:
        """
        Count active palette colors.

        Purpose:
            Enforce the invariant that at least one palette color stays active.

        Returns:
            int: Number of colors whose ``is_active`` flag is set.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(ColorPaletteRow)
            .where(ColorPaletteRow.is_active.is_(True))
        )
        return int(result.scalar_one())

    async def palette_color_is_used(self, color_id: int) -> bool:
        """
        Check whether a palette color is referenced by a project or label.

        Purpose:
            Protect colors from deletion while foreign keys reference them.

        Args:
            color_id: Palette color identifier.

        Returns:
            bool: ``True`` when any project or label, including soft-deleted
                rows, references the color.

        Notes:
            - Performs no writes and does not commit.
        """
        project_exists = select(ProjectRow.id).where(ProjectRow.color_id == color_id).exists()
        label_exists = select(LabelRow.id).where(LabelRow.color_id == color_id).exists()
        result = await self._session.execute(select(or_(project_exists, label_exists)))
        return bool(result.scalar_one())
