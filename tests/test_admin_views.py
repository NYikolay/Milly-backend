from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqladmin import Admin, ModelView
from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    BooleanFilter,
    ForeignKeyFilter,
    OperationColumnFilter,
)
from starlette.applications import Starlette

from backend.admin.views import AuthSessionAdmin, TelegramProfileAdmin, UserAdmin
from backend.models.auth_session import AuthSession
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


def test_views_are_bound_to_models() -> None:
    """Bind every view to its ORM model and stable identity."""
    assert UserAdmin.model is User
    assert TelegramProfileAdmin.model is TelegramProfile
    assert AuthSessionAdmin.model is AuthSession
    assert [UserAdmin.identity, TelegramProfileAdmin.identity, AuthSessionAdmin.identity] == [
        "user",
        "telegram-profile",
        "auth-session",
    ]


def _column_keys(columns: Sequence[Any]) -> list[str]:
    """Return column keys for comparisons without SQLAlchemy expressions."""
    return [column.key for column in columns]


def test_profile_view_has_search_and_filters() -> None:
    """Expose basic profile search and filters."""
    searchable = _column_keys(TelegramProfileAdmin.column_searchable_list)
    assert "telegram_id" in searchable
    assert "username" in searchable
    assert "first_name" in searchable
    assert "last_name" in searchable
    assert any(isinstance(item, BooleanFilter) for item in TelegramProfileAdmin.column_filters)
    assert any(
        isinstance(item, AllUniqueStringValuesFilter)
        for item in TelegramProfileAdmin.column_filters
    )
    assert any(isinstance(item, ForeignKeyFilter) for item in TelegramProfileAdmin.column_filters)
    assert any(
        isinstance(item, OperationColumnFilter) for item in TelegramProfileAdmin.column_filters
    )


def test_auth_session_view_hides_refresh_token_hash() -> None:
    """Keep the refresh-token hash out of the list and details views."""
    assert "refresh_token_hash" not in _column_keys(AuthSessionAdmin.column_list)
    assert "refresh_token_hash" not in _column_keys(AuthSessionAdmin.column_searchable_list)
    assert "refresh_token_hash" in _column_keys(AuthSessionAdmin.column_details_exclude_list)
    assert "user_id" in _column_keys(AuthSessionAdmin.column_list)
    assert "family_id" in _column_keys(AuthSessionAdmin.column_searchable_list)


def test_views_include_primary_keys_in_forms() -> None:
    """Keep explicit form_include_pk enabled on every model view."""
    assert UserAdmin.form_include_pk is True
    assert TelegramProfileAdmin.form_include_pk is True
    assert AuthSessionAdmin.form_include_pk is True


def test_views_register_on_admin() -> None:
    """Register all three views with stable identities."""
    admin = Admin(app=Starlette(), engine=None)
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)
    assert [view.identity for view in admin.views] == [
        "user",
        "telegram-profile",
        "auth-session",
    ]


async def test_forms_include_foreign_keys_without_primary_keys() -> None:
    """Include FK fields in forms while hiding primary keys and relations."""
    admin = Admin(app=Starlette(), engine=None)
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)

    profile_view = next(view for view in admin.views if view.identity == "telegram-profile")
    assert isinstance(profile_view, ModelView)
    profile_form = await profile_view.scaffold_form()
    profile_fields = profile_form()._fields
    assert "user_id" in profile_fields
    assert "id" not in profile_fields
    assert "user" not in profile_fields

    session_view = next(view for view in admin.views if view.identity == "auth-session")
    assert isinstance(session_view, ModelView)
    session_form = await session_view.scaffold_form()
    session_fields = session_form()._fields
    assert "user_id" in session_fields
    assert "refresh_token_hash" in session_fields
    assert "id" not in session_fields

    user_view = next(view for view in admin.views if view.identity == "user")
    assert isinstance(user_view, ModelView)
    user_form = await user_view.scaffold_form()
    user_fields = user_form()._fields
    assert "id" not in user_fields
    assert "telegram_profile" not in user_fields
