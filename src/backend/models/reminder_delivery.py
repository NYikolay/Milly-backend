from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base
from backend.domain.enums import DeliveryStatus

_STATUS_CHECK = f"status IN ({', '.join(repr(item.value) for item in DeliveryStatus)})"


class ReminderDelivery(Base):
    """Idempotent delivery attempt for a scheduled task reminder."""

    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "reminder_id",
            "scheduled_for",
            name="uq_reminder_deliveries_reminder_id_scheduled_for",
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(_STATUS_CHECK, name="status"),
        ForeignKeyConstraint(
            ["reminder_id", "user_id"],
            ["task_reminders.id", "task_reminders.user_id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_reminder_deliveries_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_reminder_deliveries_user_id_scheduled_for",
            "user_id",
            "scheduled_for",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    reminder_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        server_default=text("'pending'"),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger,
        server_default=text("0"),
        default=0,
        nullable=False,
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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
