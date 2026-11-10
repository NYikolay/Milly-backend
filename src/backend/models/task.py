from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.domain.enums import Priority

_PRIORITY_CHECK = f"priority IN ({', '.join(repr(item.value) for item in Priority)})"


class Task(Base):
    """One-off or series-generated task owned by a user."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_tasks_id_user_id"),
        CheckConstraint(_PRIORITY_CHECK, name="priority"),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["series_id", "user_id"],
            ["task_series.id", "task_series.user_id"],
            ondelete="RESTRICT",
        ),
        Index(
            "uq_tasks_series_id_due_at",
            "series_id",
            "due_at",
            unique=True,
            postgresql_where=text("series_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_user_id_due_at_id",
            "user_id",
            "due_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_user_id_completed_at",
            "user_id",
            "completed_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_user_id_project_id_due_at",
            "user_id",
            "project_id",
            "due_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_user_id_priority_due_at",
            "user_id",
            "priority",
            "due_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_user_id_series_id",
            "user_id",
            "series_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_project_id_completed_at",
            "project_id",
            "completed_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    series_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(32),
        server_default=text("'neither'"),
        nullable=False,
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    reminders_enabled: Mapped[bool] = mapped_column(
        Boolean,
        server_default=false(),
        default=False,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deletion_batch_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
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
