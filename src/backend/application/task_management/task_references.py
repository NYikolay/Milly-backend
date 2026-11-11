from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.application.ports.collaborators import LabelReader, ProjectReader
from backend.application.ports.task_management import AccountRepository
from backend.core.errors import BadRequestError, NotFoundError

_PROJECT_NOT_FOUND = "Project not found"
_PROJECT_ARCHIVED = "Project is archived"
_LABEL_NOT_FOUND = "Label not found"
_INVALID_TIMEZONE = "Invalid timezone"

__all__ = ["TaskReferences"]


class TaskReferences:
    """Validates task project and label references and resolves timezones."""

    def __init__(
        self,
        project_reader: ProjectReader,
        label_reader: LabelReader,
        account_repository: AccountRepository,
    ) -> None:
        """
        Initialize the reference validator with read-only collaborators.

        Purpose:
            Share project, label, and timezone resolution between task and
            series use cases without duplicating validation rules.

        Args:
            project_reader: Project ownership and lifecycle lookups.
            label_reader: Label ownership and existence lookups.
            account_repository: User timezone lookups.
        """
        self._project_reader = project_reader
        self._label_reader = label_reader
        self._account_repository = account_repository

    async def validate_project(self, user_id: int, project_id: int | None) -> None:
        """
        Validate an optional project reference.

        Purpose:
            Reject missing, cross-user, deleted, or archived projects before
            writing.

        Args:
            user_id: Owner identifier.
            project_id: Project identifier or ``None``.

        Returns:
            None: The project is valid or absent.

        Raises:
            NotFoundError: If the project does not exist for this user.
            BadRequestError: If the project is archived.

        Notes:
            - Performs no writes and does not commit.
        """
        if project_id is None:
            return
        project = await self._project_reader.get_project(user_id, project_id)
        if project is None:
            raise NotFoundError(_PROJECT_NOT_FOUND)
        if project.archived_at is not None:
            raise BadRequestError(_PROJECT_ARCHIVED)

    async def validate_labels(
        self,
        user_id: int,
        label_ids: Sequence[int],
    ) -> tuple[int, ...]:
        """
        Validate a label selection and remove duplicates.

        Purpose:
            Ensure every assigned label exists for the user before writing.

        Args:
            user_id: Owner identifier.
            label_ids: Requested label identifiers.

        Returns:
            tuple[int, ...]: Unique owned label identifiers in input order.

        Raises:
            NotFoundError: If any label does not exist for this user.

        Notes:
            - Performs one batch ownership and existence query.
            - Performs no writes and does not commit.
        """
        unique_ids = tuple(dict.fromkeys(label_ids))
        labels = await self._label_reader.get_labels_by_ids(user_id, unique_ids)
        if len(labels) != len(unique_ids):
            raise NotFoundError(_LABEL_NOT_FOUND)
        return unique_ids

    async def user_timezone(self, user_id: int) -> ZoneInfo:
        """
        Resolve the user's IANA timezone.

        Purpose:
            Compute local task-day bounds and scheduled reminder slots.

        Args:
            user_id: Owner identifier.

        Returns:
            ZoneInfo: The user's timezone, defaulting to UTC when unset.

        Raises:
            BadRequestError: If the stored timezone is invalid.

        Notes:
            - Performs no writes and does not commit.
        """
        name = await self._account_repository.get_user_timezone(user_id)
        try:
            return ZoneInfo(name or "UTC")
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise BadRequestError(_INVALID_TIMEZONE) from error

    def named_timezone(self, name: str) -> ZoneInfo:
        """
        Resolve an IANA timezone name.

        Purpose:
            Validate series and reminder timezone configuration.

        Args:
            name: IANA timezone name.

        Returns:
            ZoneInfo: The resolved timezone.

        Raises:
            BadRequestError: If the timezone name is invalid.

        Notes:
            - Performs no writes and does not commit.
        """
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise BadRequestError(_INVALID_TIMEZONE) from error
