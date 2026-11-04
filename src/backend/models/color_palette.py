from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    SmallInteger,
    String,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class ColorPalette(Base):
    """Global color palette entry referenced by projects and labels."""

    __tablename__ = "color_palette"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9_-]+$'", name="slug_format"),
        CheckConstraint("hex ~ '^#[0-9A-Fa-f]{6}$'", name="hex_format"),
        Index(
            "ix_color_palette_is_active_sort_order_id",
            "is_active",
            "sort_order",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    hex: Mapped[str] = mapped_column(CHAR(7), nullable=False)
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        server_default=true(),
        default=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
