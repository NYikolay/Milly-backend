from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest
from sqladmin import Admin, ModelView
from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    BooleanFilter,
    ForeignKeyFilter,
    OperationColumnFilter,
)
from starlette.applications import Starlette

from backend.admin.views import (
    AuthSessionAdmin,
    ColorPaletteAdmin,
    TelegramProfileAdmin,
    UserAdmin,
)
from backend.core.errors import ConflictError
from backend.models.auth_session import AuthSession
from backend.models.color_palette import ColorPalette
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


def test_views_are_bound_to_models() -> None:
    """Bind every view to its ORM model and stable identity."""
    assert UserAdmin.model is User
    assert TelegramProfileAdmin.model is TelegramProfile
    assert AuthSessionAdmin.model is AuthSession
    assert ColorPaletteAdmin.model is ColorPalette
    assert [
        ColorPaletteAdmin.identity,
        UserAdmin.identity,
        TelegramProfileAdmin.identity,
        AuthSessionAdmin.identity,
    ] == [
        "color-palette",
        "user",
        "telegram-profile",
        "auth-session",
    ]


def _column_keys(columns: Sequence[Any]) -> list[str]:
    """Return column keys for comparisons without SQLAlchemy expressions."""
    return [column.key for column in columns]


class _FakeScalarResult:
    """Scalar result double returning a preset count."""

    def __init__(self, value: int) -> None:
        """Store the count returned by ``scalar_one``."""
        self._value = value

    def scalar_one(self) -> int:
        """Return the preset count."""
        return self._value


class _FakeSession:
    """Async session double recording statements and returning a count."""

    def __init__(self, count: int) -> None:
        """Store the count and initialize the statement log."""
        self.count = count
        self.statements: list[Any] = []

    async def __aenter__(self) -> _FakeSession:
        """Return the session from an async context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit the async context manager without suppressing errors."""
        return None

    async def execute(self, statement: Any) -> _FakeScalarResult:
        """Record a statement and return the preset count."""
        self.statements.append(statement)
        return _FakeScalarResult(self.count)


class _FakeSessionMaker:
    """Session factory double yielding one configured fake session."""

    def __init__(self, count: int) -> None:
        """Create the single fake session with the given count."""
        self.session = _FakeSession(count)

    def __call__(self, **kwargs: Any) -> _FakeSession:
        """Return the configured fake session."""
        return self.session


def _palette_view(count: int) -> tuple[ColorPaletteAdmin, _FakeSession]:
    """Build a palette view wired to a fake session returning `count`."""
    view = ColorPaletteAdmin()
    session_maker = _FakeSessionMaker(count)
    cast(Any, view).session_maker = session_maker
    return view, session_maker.session


def _palette(**overrides: Any) -> ColorPalette:
    """Build a palette color instance for hook tests."""
    values: dict[str, Any] = {
        "id": 1,
        "slug": "grape",
        "name": "Виноградный",
        "hex": "#BB6BD9",
        "sort_order": 1,
        "is_active": True,
    }
    values.update(overrides)
    return ColorPalette(**values)


def test_palette_view_exposes_columns_search_sort_and_filters() -> None:
    """Expose palette columns, search, sorting, and filters."""
    assert _column_keys(ColorPaletteAdmin.column_list) == [
        "id",
        "slug",
        "name",
        "hex",
        "sort_order",
        "is_active",
    ]
    assert _column_keys(ColorPaletteAdmin.column_searchable_list) == ["slug", "name"]
    assert _column_keys(ColorPaletteAdmin.column_sortable_list) == [
        "id",
        "sort_order",
        "name",
    ]
    assert ColorPaletteAdmin.column_default_sort[0].key == "sort_order"
    assert ColorPaletteAdmin.column_default_sort[1] is False
    assert _column_keys(ColorPaletteAdmin.form_excluded_columns) == ["id"]
    assert any(isinstance(item, BooleanFilter) for item in ColorPaletteAdmin.column_filters)
    assert any(isinstance(item, OperationColumnFilter) for item in ColorPaletteAdmin.column_filters)


async def test_palette_change_rejects_slug_change() -> None:
    """Reject changing the stable slug after creation."""
    view, _ = _palette_view(10)
    with pytest.raises(ConflictError, match="Palette color slug is immutable"):
        await view.on_model_change(
            {"slug": "blue", "is_active": True},
            _palette(),
            False,
            cast(Any, None),
        )


async def test_palette_change_allows_slug_on_create() -> None:
    """Allow choosing a slug when the color is created."""
    view, _ = _palette_view(10)
    await view.on_model_change(
        {"slug": "blue", "is_active": True},
        _palette(),
        True,
        cast(Any, None),
    )


async def test_palette_change_rejects_deactivating_last_active() -> None:
    """Reject deactivation when no other active color remains."""
    view, session = _palette_view(0)
    with pytest.raises(ConflictError, match="Cannot deactivate the last active palette color"):
        await view.on_model_change(
            {"slug": "grape", "is_active": False},
            _palette(),
            False,
            cast(Any, None),
        )
    assert len(session.statements) == 1


async def test_palette_change_allows_deactivation_with_another_active() -> None:
    """Allow deactivation while another active color remains."""
    view, _ = _palette_view(1)
    await view.on_model_change(
        {"slug": "grape", "is_active": False},
        _palette(),
        False,
        cast(Any, None),
    )


async def test_palette_delete_rejects_last_active() -> None:
    """Reject deleting the last active color."""
    view, session = _palette_view(0)
    with pytest.raises(ConflictError, match="Cannot delete the last active palette color"):
        await view.on_model_delete(_palette(), cast(Any, None))
    assert len(session.statements) == 1


async def test_palette_delete_allows_color_with_another_active() -> None:
    """Allow deleting an active color while another active color remains."""
    view, _ = _palette_view(1)
    await view.on_model_delete(_palette(), cast(Any, None))


async def test_palette_delete_allows_inactive_color() -> None:
    """Allow deleting an inactive color without querying active colors."""
    view, session = _palette_view(0)
    await view.on_model_delete(_palette(is_active=False), cast(Any, None))
    assert session.statements == []


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
    """Register all four views with stable identities."""
    admin = Admin(app=Starlette(), engine=None)
    admin.add_view(ColorPaletteAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)
    assert [view.identity for view in admin.views] == [
        "color-palette",
        "user",
        "telegram-profile",
        "auth-session",
    ]


async def test_forms_include_foreign_keys_without_primary_keys() -> None:
    """Include FK fields in forms while hiding primary keys and relations."""
    admin = Admin(app=Starlette(), engine=None)
    admin.add_view(ColorPaletteAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)

    palette_view = next(view for view in admin.views if view.identity == "color-palette")
    assert isinstance(palette_view, ModelView)
    palette_form = await palette_view.scaffold_form()
    palette_fields = palette_form()._fields
    assert "id" not in palette_fields
    assert "slug" in palette_fields
    assert "is_active" in palette_fields

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
