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


class TaskLabel(Base):
    """Assignment of a user label to a task."""

    __tablename__ = "task_labels"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "user_id"],
            ["tasks.id", "tasks.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            ondelete="CASCADE",
        ),
        Index("ix_task_labels_label_id_task_id", "label_id", "task_id"),
        Index("ix_task_labels_user_id_label_id", "user_id", "label_id"),
    )

    task_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
