from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.task_management.enums import Priority, SeriesFrequency

_PRIORITY_CHECK = f"priority IN ({', '.join(repr(item.value) for item in Priority)})"
_FREQUENCY_CHECK = f"frequency IN ({', '.join(repr(item.value) for item in SeriesFrequency)})"
_FREQUENCY_INTERVAL_CHECK = (
    f"(frequency = {SeriesFrequency.DAILY.value!r} AND interval_days = 1)"
    f" OR (frequency = {SeriesFrequency.PERIODIC.value!r} AND interval_days >= 2)"
)


class TaskSeries(Base):
    """Recurring daily or periodic task template owned by a user."""

    __tablename__ = "task_series"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_task_series_id_user_id"),
        CheckConstraint(_FREQUENCY_CHECK, name="frequency"),
        CheckConstraint("interval_days >= 1", name="interval_days_positive"),
        CheckConstraint(_FREQUENCY_INTERVAL_CHECK, name="frequency_interval"),
        CheckConstraint("ends_on IS NULL OR ends_on >= starts_on", name="ends_on_after_starts_on"),
        CheckConstraint(_PRIORITY_CHECK, name="priority"),
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_task_series_user_id_generated_through_active",
            "user_id",
            "generated_through",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_task_series_generated_through_active",
            "generated_through",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        String(32),
        server_default=text("'neither'"),
        nullable=False,
    )
    frequency: Mapped[str] = mapped_column(
        String(16),
        server_default=text("'daily'"),
        nullable=False,
    )
    interval_days: Mapped[int] = mapped_column(
        SmallInteger,
        server_default=text("1"),
        default=1,
        nullable=False,
    )
    time_of_day: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    generated_through: Mapped[datetime | None] = mapped_column(
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
