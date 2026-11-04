from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class SeriesLabel(Base):
    """Assignment of a user label to a recurring task series."""

    __tablename__ = "series_labels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["series_id", "user_id"],
            ["task_series.id", "task_series.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_series_labels_label_id_series_id", "label_id", "series_id"),
        Index("ix_series_labels_user_id_label_id", "user_id", "label_id"),
    )

    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
