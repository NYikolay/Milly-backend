from __future__ import annotations

from sqlalchemy import CHAR, BigInteger, DefaultClause, Identity, Uuid

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


def test_all_primary_keys_use_identity() -> None:
    for table_name in ("users", "telegram_profiles", "auth_sessions"):
        column = Base.metadata.tables[table_name].c.id
        assert isinstance(column.type, BigInteger)
        assert column.primary_key
        assert isinstance(column.identity, Identity)


def test_telegram_profile_column_types_and_defaults() -> None:
    table = Base.metadata.tables["telegram_profiles"]
    assert isinstance(table.c.telegram_id.type, BigInteger)
    assert table.c.is_premium.nullable is False
    server_default = table.c.is_premium.server_default
    assert isinstance(server_default, DefaultClause)
    assert str(server_default.arg) == "false"


def test_auth_session_column_types() -> None:
    table = Base.metadata.tables["auth_sessions"]
    assert isinstance(table.c.family_id.type, Uuid)
    assert isinstance(table.c.refresh_token_hash.type, CHAR)
    assert table.c.refresh_token_hash.type.length == 64
    assert "ix_auth_sessions_user_id" in {index.name for index in table.indexes}


def test_telegram_profile_relationship_configuration() -> None:
    user_profile = User.telegram_profile.property
    profile_user = TelegramProfile.user.property
    assert user_profile.uselist is False
    assert user_profile.back_populates == "user"
    assert profile_user.back_populates == "telegram_profile"
    assert user_profile.passive_deletes is True
    assert "delete" in user_profile.cascade


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
