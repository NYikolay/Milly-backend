from __future__ import annotations

from sqlalchemy import BigInteger

import backend.models  # noqa: F401
from backend.db.base import Base
from backend.models.auth_session import AuthSession
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


def test_auth_tables_are_registered() -> None:
    assert {"users", "telegram_profiles", "auth_sessions"} <= set(Base.metadata.tables)


def test_user_id_is_bigint_primary_key() -> None:
    table = Base.metadata.tables["users"]
    assert isinstance(table.c.id.type, BigInteger)
    assert table.c.id.primary_key


def test_telegram_profile_is_one_to_one() -> None:
    table = Base.metadata.tables["telegram_profiles"]
    assert table.c.user_id.unique is True
    assert table.c.telegram_id.unique is True
    foreign_key = next(iter(table.c.user_id.foreign_keys))
    assert foreign_key.target_fullname == "users.id"
    assert foreign_key.ondelete == "CASCADE"


def test_auth_session_constraints() -> None:
    table = Base.metadata.tables["auth_sessions"]
    assert table.c.refresh_token_hash.unique is True
    assert table.c.family_id.nullable is False
    assert table.c.expires_at.nullable is False
    assert "ix_auth_sessions_family_id" in {index.name for index in table.indexes}
    assert "ix_auth_sessions_expires_at" in {index.name for index in table.indexes}
    assert AuthSession.__tablename__ == "auth_sessions"
    assert User.__tablename__ == "users"
    assert TelegramProfile.__tablename__ == "telegram_profiles"
