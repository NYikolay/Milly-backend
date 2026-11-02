from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, DateTime, ForeignKey, Identity, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class AuthSession(Base):
    """Refresh-token session used for rotation and reuse detection."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    family_id: Mapped[UUID] = mapped_column(Uuid, index=True, nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
