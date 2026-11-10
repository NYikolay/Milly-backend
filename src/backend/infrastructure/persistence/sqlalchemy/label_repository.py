"""SQLAlchemy implementation of the label persistence port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.entities import Label
from backend.infrastructure.persistence.sqlalchemy.mappers import (
    apply_label,
    label_to_entity,
)
from backend.models.label import Label as LabelRow


class SqlAlchemyLabelRepository:
    """SQLAlchemy implementation of the label persistence contract."""

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialize the label adapter with a request-scoped session.

        Purpose:
            Bind label reads and writes to the caller's unit of work.

        Args:
            session: Async SQLAlchemy session owned by the caller.

        Notes:
            - The adapter never closes the session or commits; the caller
              controls the transaction boundary.
        """
        self._session = session

    async def list_labels(self, user_id: int) -> Sequence[Label]:
        """
        List non-deleted labels owned by a user.

        Purpose:
            Power label selection and label management.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[Label]: Non-deleted domain labels ordered by
                case-insensitive name and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        result = await self._session.execute(
            select(LabelRow)
            .where(
                LabelRow.user_id == user_id,
                LabelRow.deleted_at.is_(None),
            )
            .order_by(func.lower(LabelRow.name), LabelRow.id)
        )
        return [label_to_entity(row) for row in result.scalars().all()]

    async def get_label(
        self,
        user_id: int,
        label_id: int,
        *,
        for_update: bool = False,
    ) -> Label | None:
        """
        Load a non-deleted label owned by a user.

        Purpose:
            Resolve a label for reads and serialize mutations when requested.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Label | None: The matching domain label, or ``None`` including
                when it belongs to another user or is soft-deleted.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(LabelRow).where(
            LabelRow.id == label_id,
            LabelRow.user_id == user_id,
            LabelRow.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        row = result.scalar_one_or_none()
        return label_to_entity(row) if row is not None else None

    async def get_labels_by_ids(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> Sequence[Label]:
        """
        Load multiple non-deleted labels owned by a user.

        Purpose:
            Validate label selections before assigning them to tasks or series.

        Args:
            user_id: Owner identifier.
            label_ids: Requested label identifiers.

        Returns:
            Sequence[Label]: Matching owned domain labels ordered by
                identifier; unknown or soft-deleted identifiers are omitted.

        Notes:
            - Returns an empty sequence without a query for an empty input.
            - Performs no writes and does not commit.
        """
        if not label_ids:
            return []
        result = await self._session.execute(
            select(LabelRow)
            .where(
                LabelRow.user_id == user_id,
                LabelRow.deleted_at.is_(None),
                LabelRow.id.in_(label_ids),
            )
            .order_by(LabelRow.id)
        )
        return [label_to_entity(row) for row in result.scalars().all()]

    async def label_name_exists(
        self,
        user_id: int,
        name: str,
        *,
        exclude_label_id: int | None = None,
    ) -> bool:
        """
        Check for an active label name owned by a user.

        Purpose:
            Enforce case-insensitive uniqueness among non-deleted labels before
            writing.

        Args:
            user_id: Owner identifier.
            name: Candidate label name, already trimmed by the caller.
            exclude_label_id: Label to ignore during the comparison.

        Returns:
            bool: ``True`` when another non-deleted label has the same
                case-insensitive name.

        Notes:
            - Performs no writes and does not commit.
        """
        statement = select(LabelRow.id).where(
            LabelRow.user_id == user_id,
            LabelRow.deleted_at.is_(None),
            func.lower(LabelRow.name) == name.lower(),
        )
        if exclude_label_id is not None:
            statement = statement.where(LabelRow.id != exclude_label_id)
        result = await self._session.execute(statement.limit(1))
        return result.scalar_one_or_none() is not None

    async def add_label(self, label: Label) -> Label:
        """
        Persist a new label.

        Purpose:
            Store a validated label in the current unit of work.

        Args:
            label: Domain label to insert.

        Returns:
            Label: The flushed label mapped back from its ORM row with the
                generated identifier.

        Notes:
            - Inserts a fresh ORM row and never copies the transient domain
              identifier, so the database identity generates the key.
            - Flushes but does not commit.
        """
        row = _new_label_row(label)
        self._session.add(row)
        await self._session.flush()
        return label_to_entity(row)

    async def save_label(self, label: Label) -> None:
        """
        Persist changes to an existing label.

        Purpose:
            Make service-level mutations of a loaded domain label durable in
            the current unit of work.

        Args:
            label: Label whose persisted fields changed.

        Returns:
            None: The change stays in the current unit of work.

        Raises:
            LookupError: If the label row is not present in the session.

        Notes:
            - A transient label with ``id == 0`` is inserted through
              ``add_label``.
            - Resolves the tracked row by primary key; the caller is
              responsible for loading the entity through a tenant-scoped read.
            - Flushes but does not commit.
        """
        if label.id == 0:
            await self.add_label(label)
            return
        row = await self._session.get(LabelRow, label.id)
        if row is None:
            raise LookupError(f"Label {label.id} is not present in the session")
        apply_label(label, row)
        await self._session.flush()

    async def soft_delete_label(self, label: Label, deleted_at: datetime) -> None:
        """
        Soft-delete a label.

        Purpose:
            Hide a label from selection and task responses while preserving
            its task and series assignments.

        Args:
            label: Domain label to delete.
            deleted_at: Deletion timestamp.

        Returns:
            None: The change stays in the current unit of work.

        Notes:
            - Updates the label row by primary key, matching the previous
              ORM-flush behavior for an already tenant-scoped entity.
            - Flushes but does not commit; the UnitOfWork owns the transaction.
        """
        label.deleted_at = deleted_at
        await self._session.execute(
            update(LabelRow)
            .where(LabelRow.id == label.id)
            .values(deleted_at=deleted_at)
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()


def _new_label_row(label: Label) -> LabelRow:
    """
    Build a fresh ORM label row from a domain label.

    Purpose:
        Insert new labels without copying the transient domain identifier, so
        PostgreSQL can generate the primary key.

    Args:
        label: Domain label to insert.

    Returns:
        LabelRow: A transient ORM row populated with persisted fields.

    Notes:
        - The returned row is not added to a session and has no identifier.
    """
    return LabelRow(
        user_id=label.user_id,
        name=label.name,
        color_id=label.color_id,
        deleted_at=label.deleted_at,
        created_at=label.created_at,
        updated_at=label.updated_at,
    )
