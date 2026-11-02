from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, Identity, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base

if TYPE_CHECKING:
    from backend.models.user import User


class TelegramProfile(Base):
    """Telegram identity linked one-to-one with an application user."""

    __tablename__ = "telegram_profiles"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    is_bot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    first_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_premium: Mapped[bool] = mapped_column(
        Boolean,
        server_default=false(),
        nullable=False,
    )
    added_to_attachment_menu: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allows_write_to_pm: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="telegram_profile")
