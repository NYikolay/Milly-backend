from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from backend.application.ports.collaborators import PalettePolicy
from backend.application.ports.task_management import LabelRepository
from backend.core.errors import BadRequestError, ConflictError, NotFoundError
from backend.domain.entities import Label

_LABEL_NOT_FOUND = "Label not found"
_LABEL_NAME_EXISTS = "Label name already exists"
_LABEL_NAME_REQUIRED = "Label name is required"


class LabelService:
    """Coordinates label lifecycle operations and per-user name rules."""

    def __init__(
        self,
        repository: LabelRepository,
        palette_service: PalettePolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Initialize the service with its persistence, palette, and time ports.

        Purpose:
            Keep label rules independent of request handling and make
            timestamps deterministic through an injectable clock.

        Args:
            repository: Persistence operations for labels.
            palette_service: Palette validation and default resolution.
            clock: Current-time provider. Defaults to the current UTC time.

        Notes:
            - The service never commits; the request unit of work owns the
              transaction boundary.
        """
        self._repository = repository
        self._palette_service = palette_service
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    async def list(self, user_id: int) -> Sequence[Label]:
        """
        List labels owned by a user.

        Purpose:
            Provide selectable labels for tasks and series.

        Args:
            user_id: Owner identifier.

        Returns:
            Sequence[Label]: Non-deleted labels in repository order.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._repository.list_labels(user_id)

    async def get(self, user_id: int, label_id: int) -> Label:
        """
        Load one owned label.

        Purpose:
            Resolve a label for a label-detail response or assignment check.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.

        Returns:
            Label: The matching non-deleted label.

        Raises:
            NotFoundError: If the label does not exist for this user.

        Notes:
            - Performs no writes and does not commit.
        """
        label = await self._repository.get_label(user_id, label_id)
        if label is None:
            raise NotFoundError(_LABEL_NOT_FOUND)
        return label

    async def list_by_ids(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> Sequence[Label]:
        """
        Load multiple owned labels in one query.

        Purpose:
            Validate a label selection before assigning it to tasks or series.

        Args:
            user_id: Owner identifier.
            label_ids: Label identifiers to resolve.

        Returns:
            Sequence[Label]: Matching non-deleted owned labels; unknown or
                soft-deleted identifiers are omitted.

        Notes:
            - Performs no writes and does not commit.
        """
        return await self._repository.get_labels_by_ids(user_id, label_ids)

    async def create(
        self,
        user_id: int,
        *,
        name: str,
        color_id: int | None = None,
    ) -> Label:
        """
        Create a label.

        Purpose:
            Validate the trimmed name and active color, then persist the label
            in the current unit of work.

        Args:
            user_id: Owner identifier.
            name: Label name; surrounding whitespace is removed.
            color_id: Palette color identifier; ``None`` selects the default
                active color.

        Returns:
            Label: The flushed label with its generated identifier.

        Raises:
            BadRequestError: If the trimmed name is empty or the color is
                missing or inactive.
            ConflictError: If another non-deleted label already uses the name.

        Notes:
            - Flushes through the repository but does not commit.
        """
        normalized_name = self._normalize_name(name)
        if await self._repository.label_name_exists(user_id, normalized_name):
            raise ConflictError(_LABEL_NAME_EXISTS)
        color = await self._palette_service.resolve_for_write(color_id)
        label = Label(
            user_id=user_id,
            name=normalized_name,
            color_id=color.id,
        )
        return await self._repository.add_label(label)

    async def update(
        self,
        user_id: int,
        label_id: int,
        *,
        name: str | None = None,
        color_id: int | None = None,
    ) -> Label:
        """
        Update a label.

        Purpose:
            Apply name and color changes while keeping non-deleted label names
            unique for the user.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.
            name: New name; ``None`` keeps the current name. Surrounding
                whitespace is removed.
            color_id: New active palette color; ``None`` keeps the current
                color.

        Returns:
            Label: The updated label in the current unit of work.

        Raises:
            NotFoundError: If the label does not exist for this user.
            BadRequestError: If the trimmed name is empty or the color is
                missing or inactive.
            ConflictError: If another non-deleted label already uses the name.

        Notes:
            - Locks the label row for the duration of the transaction.
            - Persists the mutated domain entity through ``save_label`` but
              does not commit; the request dependency owns the transaction.
        """
        label = await self._require_label(user_id, label_id, for_update=True)
        if name is not None:
            normalized_name = self._normalize_name(name)
            if await self._repository.label_name_exists(
                user_id,
                normalized_name,
                exclude_label_id=label.id,
            ):
                raise ConflictError(_LABEL_NAME_EXISTS)
            label.name = normalized_name
        if color_id is not None:
            color = await self._palette_service.ensure_active(color_id)
            label.color_id = color.id
        await self._repository.save_label(label)
        return label

    async def delete(self, user_id: int, label_id: int) -> None:
        """
        Soft-delete a label.

        Purpose:
            Hide a label from selection and responses while preserving its
            task and series assignments.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.

        Returns:
            None: The label is deleted or was already absent.

        Notes:
            - Deletion is idempotent: a missing, already deleted, or other
              user's label succeeds without a write.
            - Locks the label row and does not commit.
        """
        label = await self._repository.get_label(user_id, label_id, for_update=True)
        if label is None:
            return
        await self._repository.soft_delete_label(label, self._clock())

    async def _require_label(
        self,
        user_id: int,
        label_id: int,
        *,
        for_update: bool = False,
    ) -> Label:
        """
        Load a label or raise a not-found error.

        Purpose:
            Treat missing labels and another user's labels identically.

        Args:
            user_id: Owner identifier.
            label_id: Label identifier.
            for_update: Whether to lock the row until the transaction ends.

        Returns:
            Label: The matching non-deleted label.

        Raises:
            NotFoundError: If no label matches the user and identifier.

        Notes:
            - Performs no writes and does not commit.
        """
        label = await self._repository.get_label(
            user_id,
            label_id,
            for_update=for_update,
        )
        if label is None:
            raise NotFoundError(_LABEL_NOT_FOUND)
        return label

    def _normalize_name(self, name: str) -> str:
        """
        Trim a label name and reject an empty result.

        Purpose:
            Apply the shared label name rule before uniqueness checks and
            writes.

        Args:
            name: Candidate label name.

        Returns:
            str: The trimmed name.

        Raises:
            BadRequestError: If the trimmed name is empty.
        """
        normalized = name.strip()
        if not normalized:
            raise BadRequestError(_LABEL_NAME_REQUIRED)
        return normalized
