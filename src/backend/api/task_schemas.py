from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ColorResponse(BaseModel):
    """Palette color exposed to clients."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    hex: str


class ColorPaletteResponse(BaseModel):
    """Selectable palette colors in display order."""

    items: list[ColorResponse]


class ProjectCreate(BaseModel):
    """Fields accepted when creating a project."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    color_id: int | None = Field(default=None, gt=0)
    icon: str | None = None

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        """
        Trim a project name and reject an empty result.

        Purpose:
            Apply the shared project name rule at the request boundary.

        Args:
            value: Candidate project name.

        Returns:
            str: The trimmed name.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name must not be empty")
        return normalized


class ProjectUpdate(BaseModel):
    """Optional fields accepted when updating a project."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    color_id: int | None = Field(default=None, gt=0)
    icon: str | None = None

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        """
        Trim an optional project name and reject an empty result.

        Purpose:
            Apply the shared project name rule while preserving omitted fields.

        Args:
            value: Candidate project name, or ``None``.

        Returns:
            str | None: The trimmed name or ``None``.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Project name must not be empty")
        return normalized


class ProjectProgressResponse(BaseModel):
    """Task completion counters for one project."""

    total: int
    completed: int
    remaining: int


class ProjectResponse(BaseModel):
    """Project with its palette color and progress counters."""

    id: int
    name: str
    description: str | None
    icon: str | None
    color: ColorResponse
    progress: ProjectProgressResponse
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """Projects visible to the authenticated user."""

    items: list[ProjectResponse]


class LabelCreate(BaseModel):
    """Fields accepted when creating a label."""

    name: str = Field(min_length=1, max_length=60)
    color_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        """
        Trim a label name and reject an empty result.

        Purpose:
            Apply the shared label name rule at the request boundary.

        Args:
            value: Candidate label name.

        Returns:
            str: The trimmed name.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("Label name must not be empty")
        return normalized


class LabelUpdate(BaseModel):
    """Optional fields accepted when updating a label."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    color_id: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        """
        Trim an optional label name and reject an empty result.

        Purpose:
            Apply the shared label name rule while preserving omitted fields.

        Args:
            value: Candidate label name, or ``None``.

        Returns:
            str | None: The trimmed name or ``None``.

        Raises:
            ValueError: If the trimmed name is empty.
        """
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Label name must not be empty")
        return normalized


class LabelResponse(BaseModel):
    """Label with its resolved palette color."""

    id: int
    name: str
    color: ColorResponse
    created_at: datetime
    updated_at: datetime


class LabelListResponse(BaseModel):
    """Labels owned by the authenticated user."""

    items: list[LabelResponse]
