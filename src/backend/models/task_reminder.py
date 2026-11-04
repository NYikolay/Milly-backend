from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.task_management.enums import ReminderKind

_KIND_CHECK = f"kind IN ({', '.join(repr(item.value) for item in ReminderKind)})"


class TaskReminder(Base):
    """Reminder rule attached to a task or a recurring series."""

    __tablename__ = "task_reminders"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_task_reminders_id_user_id"),
        CheckConstraint(
            "(CASE WHEN task_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN series_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="one_owner",
        ),
        CheckConstraint(_KIND_CHECK, name="kind"),
        CheckConstraint("offset_minutes >= 0", name="offset_minutes_non_negative"),
        CheckConstraint("interval_days > 0", name="interval_days_positive"),
        ForeignKeyConstraint(
            ["task_id", "user_id"],
            ["tasks.id", "tasks.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["series_id", "user_id"],
            ["task_series.id", "task_series.user_id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_task_reminders_task_id_kind",
            "task_id",
            "kind",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
        ),
        Index(
            "uq_task_reminders_series_id_kind",
            "series_id",
            "kind",
            unique=True,
            postgresql_where=text("series_id IS NOT NULL"),
        ),
        Index(
            "ix_task_reminders_next_run_at",
            "next_run_at",
            postgresql_where=text("enabled AND next_run_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    series_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        server_default=true(),
        default=True,
        nullable=False,
    )
    offset_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    time_of_day: Mapped[time | None] = mapped_column(Time, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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
