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
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from starlette.applications import Starlette

from backend.admin.views import (
    AuthSessionAdmin,
    ColorPaletteAdmin,
    LabelAdmin,
    ProjectAdmin,
    TaskAdmin,
    TaskSeriesAdmin,
    TelegramProfileAdmin,
    UserAdmin,
)
from backend.application.task_management.palette import PaletteService
from backend.core.errors import ConflictError
from backend.models.auth_session import AuthSession
from backend.models.color_palette import ColorPalette
from backend.models.label import Label
from backend.models.project import Project
from backend.models.task import Task
from backend.models.task_series import TaskSeries
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


def test_views_are_bound_to_models() -> None:
    """Bind every view to its ORM model and stable identity."""
    assert UserAdmin.model is User
    assert TelegramProfileAdmin.model is TelegramProfile
    assert AuthSessionAdmin.model is AuthSession
    assert ColorPaletteAdmin.model is ColorPalette
    assert ProjectAdmin.model is Project
    assert LabelAdmin.model is Label
    assert TaskAdmin.model is Task
    assert TaskSeriesAdmin.model is TaskSeries
    assert [
        ColorPaletteAdmin.identity,
        UserAdmin.identity,
        TelegramProfileAdmin.identity,
        AuthSessionAdmin.identity,
        ProjectAdmin.identity,
        LabelAdmin.identity,
        TaskAdmin.identity,
        TaskSeriesAdmin.identity,
    ] == [
        "color-palette",
        "user",
        "telegram-profile",
        "auth-session",
        "project",
        "label",
        "task",
        "task-series",
    ]


def _column_keys(columns: Sequence[Any]) -> list[str]:
    """Return column keys for comparisons without SQLAlchemy expressions."""
    return [column.key for column in columns]


class _FakeSession:
    """Async session double used only to satisfy the admin session factory."""

    async def __aenter__(self) -> _FakeSession:
        """Return the session from an async context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit the async context manager without suppressing errors."""
        return None


class _FakeSessionMaker:
    """Session factory double yielding one fake session."""

    def __init__(self) -> None:
        """Create the single fake session."""
        self.session = _FakeSession()

    def __call__(self, **kwargs: Any) -> _FakeSession:
        """Return the configured fake session."""
        return self.session


def _palette_view() -> tuple[ColorPaletteAdmin, _FakeSession]:
    """Build a palette view wired to a fake session factory."""
    view = ColorPaletteAdmin()
    session_maker = _FakeSessionMaker()
    cast(Any, view).session_maker = session_maker
    return view, session_maker.session


def _compile_statement(statement: Any) -> str:
    """Compile a recorded statement with PostgreSQL literal binds."""
    return str(
        statement.compile(
            dialect=cast(Any, postgresql.dialect)(),
            compile_kwargs={"literal_binds": True},
        )
    )


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


def test_task_view_exposes_columns_search_sort_and_filters() -> None:
    """Expose task columns, search, sorting, and filters."""
    assert _column_keys(TaskAdmin.column_list) == [
        "id",
        "user_id",
        "series_id",
        "project_id",
        "title",
        "priority",
        "due_at",
        "completed_at",
        "reminders_enabled",
        "deleted_at",
        "created_at",
    ]
    assert _column_keys(TaskAdmin.column_searchable_list) == ["id", "user_id", "title"]
    assert _column_keys(TaskAdmin.column_sortable_list) == [
        "id",
        "priority",
        "due_at",
        "completed_at",
        "created_at",
    ]
    assert TaskAdmin.column_default_sort[0].key == "id"
    assert TaskAdmin.column_default_sort[1] is True
    assert _column_keys(TaskAdmin.form_excluded_columns) == ["id"]
    filters = TaskAdmin.column_filters
    assert any(isinstance(item, BooleanFilter) for item in filters)
    assert any(isinstance(item, AllUniqueStringValuesFilter) for item in filters)
    assert sum(isinstance(item, ForeignKeyFilter) for item in filters) == 3


def test_task_series_view_exposes_columns_search_sort_and_filters() -> None:
    """Expose task-series columns, search, sorting, and filters."""
    assert _column_keys(TaskSeriesAdmin.column_list) == [
        "id",
        "user_id",
        "project_id",
        "title",
        "priority",
        "frequency",
        "interval_days",
        "time_of_day",
        "timezone",
        "starts_on",
        "ends_on",
        "generated_through",
        "reminders_enabled",
        "deleted_at",
        "created_at",
    ]
    assert _column_keys(TaskSeriesAdmin.column_searchable_list) == [
        "id",
        "user_id",
        "title",
        "timezone",
    ]
    assert _column_keys(TaskSeriesAdmin.column_sortable_list) == [
        "id",
        "priority",
        "frequency",
        "starts_on",
        "ends_on",
        "generated_through",
        "created_at",
    ]
    assert TaskSeriesAdmin.column_default_sort[0].key == "id"
    assert TaskSeriesAdmin.column_default_sort[1] is True
    assert _column_keys(TaskSeriesAdmin.form_excluded_columns) == ["id"]
    filters = TaskSeriesAdmin.column_filters
    assert any(isinstance(item, BooleanFilter) for item in filters)
    assert any(isinstance(item, AllUniqueStringValuesFilter) for item in filters)
    assert sum(isinstance(item, ForeignKeyFilter) for item in filters) == 2


def test_project_view_exposes_columns_search_sort_and_filters() -> None:
    """Expose project columns, search, sorting, and filters."""
    assert _column_keys(ProjectAdmin.column_list) == [
        "id",
        "user_id",
        "name",
        "description",
        "color_id",
        "icon",
        "archived_at",
        "deleted_at",
        "created_at",
    ]
    assert _column_keys(ProjectAdmin.column_searchable_list) == ["id", "user_id", "name"]
    assert _column_keys(ProjectAdmin.column_sortable_list) == [
        "id",
        "name",
        "archived_at",
        "deleted_at",
        "created_at",
    ]
    assert ProjectAdmin.column_default_sort[0].key == "id"
    assert ProjectAdmin.column_default_sort[1] is True
    assert _column_keys(ProjectAdmin.form_excluded_columns) == ["id"]
    filters = ProjectAdmin.column_filters
    assert sum(isinstance(item, ForeignKeyFilter) for item in filters) == 2
    assert any(isinstance(item, OperationColumnFilter) for item in filters)


def test_label_view_exposes_columns_search_sort_and_filters() -> None:
    """Expose label columns, search, sorting, and filters."""
    assert _column_keys(LabelAdmin.column_list) == [
        "id",
        "user_id",
        "name",
        "color_id",
        "deleted_at",
        "created_at",
    ]
    assert _column_keys(LabelAdmin.column_searchable_list) == ["id", "user_id", "name"]
    assert _column_keys(LabelAdmin.column_sortable_list) == [
        "id",
        "name",
        "deleted_at",
        "created_at",
    ]
    assert LabelAdmin.column_default_sort[0].key == "id"
    assert LabelAdmin.column_default_sort[1] is True
    assert _column_keys(LabelAdmin.form_excluded_columns) == ["id"]
    filters = LabelAdmin.column_filters
    assert sum(isinstance(item, ForeignKeyFilter) for item in filters) == 2
    assert any(isinstance(item, OperationColumnFilter) for item in filters)


async def _empty_lookup_query(statement: Any) -> list[tuple[Any, Any]]:
    """Return no rows for a filter lookup query."""
    return []


async def test_all_foreign_key_filters_render_lookups() -> None:
    """Render lookups for every FK filter without ambiguous-column errors."""
    views = [
        TelegramProfileAdmin(),
        AuthSessionAdmin(),
        ProjectAdmin(),
        LabelAdmin(),
        TaskAdmin(),
        TaskSeriesAdmin(),
    ]
    for view in views:
        for item in view.column_filters:
            if isinstance(item, ForeignKeyFilter):
                lookups = await item.lookups(
                    cast(Any, None),
                    view.model,
                    _empty_lookup_query,
                )
                assert lookups == [("__all", "All")]


async def test_task_user_filters_render_lookups_from_users_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build task user lookups from ``users.id`` despite composite tenant FKs."""
    statements: list[Any] = []

    async def capture_query(statement: Any) -> list[tuple[Any, Any]]:
        """Record the lookup query and return no rows."""
        statements.append(statement)
        return []

    monkeypatch.setattr(
        "sqladmin.filters.get_foreign_column_name",
        lambda column: "user_id",
    )
    views = [TaskAdmin(), TaskSeriesAdmin()]
    for view in views:
        user_filter = next(
            item
            for item in view.column_filters
            if isinstance(item, ForeignKeyFilter)
            and getattr(item.foreign_key, "key", None) == "user_id"
        )
        lookups = await user_filter.lookups(
            cast(Any, None),
            view.model,
            capture_query,
        )
        assert lookups == [("__all", "All")]

    assert len(statements) == len(views)
    for statement in statements:
        sql = _compile_statement(statement)
        assert "users.id" in sql
        assert "users.user_id" not in sql


async def test_palette_change_rejects_slug_change() -> None:
    """Reject changing the stable slug after creation."""
    view, _ = _palette_view()
    with pytest.raises(ConflictError, match="Palette color slug is immutable"):
        await view.on_model_change(
            {"slug": "blue", "is_active": True},
            _palette(),
            False,
            cast(Any, None),
        )


async def test_palette_change_allows_slug_on_create() -> None:
    """Allow choosing a slug when the color is created."""
    view, _ = _palette_view()
    await view.on_model_change(
        {"slug": "blue", "is_active": True},
        _palette(),
        True,
        cast(Any, None),
    )


async def test_palette_admin_deactivation_uses_palette_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin delegates last-active-color checks to the palette service."""
    called: list[int] = []

    async def fake_ensure(self: PaletteService, color_id: int) -> None:
        """Record the color id passed to the palette policy."""
        called.append(color_id)

    monkeypatch.setattr(PaletteService, "ensure_can_deactivate", fake_ensure)
    view, _ = _palette_view()
    await view.on_model_change(
        {"slug": "grape", "is_active": False},
        _palette(id=3),
        False,
        cast(Any, None),
    )
    assert called == [3]


async def test_palette_admin_delete_uses_palette_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin delegates delete checks to the palette service."""
    called: list[int] = []

    async def fake_ensure(self: PaletteService, color_id: int) -> None:
        """Record the color id passed to the palette policy."""
        called.append(color_id)

    monkeypatch.setattr(PaletteService, "ensure_can_delete", fake_ensure)
    view, _ = _palette_view()
    await view.on_model_delete(_palette(id=3), cast(Any, None))
    assert called == [3]


async def test_palette_change_rejects_deactivating_last_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the admin deactivation message when the palette policy rejects."""

    async def reject(self: PaletteService, color_id: int) -> None:
        """Reject the deactivation like the real palette policy."""
        raise ConflictError("At least one active color is required")

    monkeypatch.setattr(PaletteService, "ensure_can_deactivate", reject)
    view, _ = _palette_view()
    with pytest.raises(ConflictError, match="Cannot deactivate the last active palette color"):
        await view.on_model_change(
            {"slug": "grape", "is_active": False},
            _palette(),
            False,
            cast(Any, None),
        )


async def test_palette_change_allows_deactivation_with_another_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow deactivation when the palette policy accepts it."""
    called: list[int] = []

    async def allow(self: PaletteService, color_id: int) -> None:
        """Accept the deactivation and record the color id."""
        called.append(color_id)

    monkeypatch.setattr(PaletteService, "ensure_can_deactivate", allow)
    view, _ = _palette_view()
    await view.on_model_change(
        {"slug": "grape", "is_active": False},
        _palette(),
        False,
        cast(Any, None),
    )
    assert called == [1]


async def test_palette_delete_rejects_last_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the admin deletion message when the palette policy rejects."""

    async def reject(self: PaletteService, color_id: int) -> None:
        """Reject the deletion like the real palette policy."""
        raise ConflictError("At least one active color is required")

    monkeypatch.setattr(PaletteService, "ensure_can_delete", reject)
    view, _ = _palette_view()
    with pytest.raises(ConflictError, match="Cannot delete the last active palette color"):
        await view.on_model_delete(_palette(), cast(Any, None))


async def test_palette_delete_allows_color_with_another_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow deleting an active color when the palette policy accepts it."""
    called: list[int] = []

    async def allow(self: PaletteService, color_id: int) -> None:
        """Accept the deletion and record the color id."""
        called.append(color_id)

    monkeypatch.setattr(PaletteService, "ensure_can_delete", allow)
    view, _ = _palette_view()
    await view.on_model_delete(_palette(), cast(Any, None))
    assert called == [1]


async def test_palette_delete_allows_inactive_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow deleting an inactive color without invoking palette policy."""
    called: list[int] = []

    async def allow(self: PaletteService, color_id: int) -> None:
        """Record any policy call for the inactive color."""
        called.append(color_id)

    monkeypatch.setattr(PaletteService, "ensure_can_delete", allow)
    view, _ = _palette_view()
    await view.on_model_delete(_palette(is_active=False), cast(Any, None))
    assert called == []


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
    assert ProjectAdmin.form_include_pk is True
    assert LabelAdmin.form_include_pk is True
    assert TaskAdmin.form_include_pk is True
    assert TaskSeriesAdmin.form_include_pk is True


def test_views_register_on_admin() -> None:
    """Register all six views with stable identities."""
    admin = Admin(app=Starlette(), engine=None)
    admin.add_view(ColorPaletteAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)
    admin.add_view(ProjectAdmin)
    admin.add_view(LabelAdmin)
    admin.add_view(TaskAdmin)
    admin.add_view(TaskSeriesAdmin)
    assert [view.identity for view in admin.views] == [
        "color-palette",
        "user",
        "telegram-profile",
        "auth-session",
        "project",
        "label",
        "task",
        "task-series",
    ]


async def test_forms_include_foreign_keys_without_primary_keys() -> None:
    """Include FK fields in forms while hiding primary keys and relations."""
    admin = Admin(app=Starlette(), engine=None)
    admin.add_view(ColorPaletteAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(TelegramProfileAdmin)
    admin.add_view(AuthSessionAdmin)
    admin.add_view(ProjectAdmin)
    admin.add_view(LabelAdmin)
    admin.add_view(TaskAdmin)
    admin.add_view(TaskSeriesAdmin)

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

    project_view = next(view for view in admin.views if view.identity == "project")
    assert isinstance(project_view, ModelView)
    project_form = await project_view.scaffold_form()
    project_fields = project_form()._fields
    assert "user_id" in project_fields
    assert "color_id" in project_fields
    assert "id" not in project_fields

    label_view = next(view for view in admin.views if view.identity == "label")
    assert isinstance(label_view, ModelView)
    label_form = await label_view.scaffold_form()
    label_fields = label_form()._fields
    assert "user_id" in label_fields
    assert "color_id" in label_fields
    assert "id" not in label_fields

    user_view = next(view for view in admin.views if view.identity == "user")
    assert isinstance(user_view, ModelView)
    user_form = await user_view.scaffold_form()
    user_fields = user_form()._fields
    assert "id" not in user_fields
    assert "telegram_profile" not in user_fields

    task_view = next(view for view in admin.views if view.identity == "task")
    assert isinstance(task_view, ModelView)
    task_form = await task_view.scaffold_form()
    task_fields = task_form()._fields
    assert "user_id" in task_fields
    assert "project_id" in task_fields
    assert "series_id" in task_fields
    assert "id" not in task_fields

    series_view = next(view for view in admin.views if view.identity == "task-series")
    assert isinstance(series_view, ModelView)
    series_form = await series_view.scaffold_form()
    series_fields = series_form()._fields
    assert "user_id" in series_fields
    assert "project_id" in series_fields
    assert "id" not in series_fields


async def test_palette_delete_translates_reference_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate a referenced-color foreign-key failure into a conflict."""
    view = ColorPaletteAdmin()
    original = ModelView.delete_model

    async def failing_delete(self: ModelView, request: Any, pk: Any) -> None:
        """Raise the database-style integrity failure under test."""
        raise IntegrityError("delete", None, RuntimeError("referenced"))

    monkeypatch.setattr(ModelView, "delete_model", failing_delete)
    try:
        with pytest.raises(ConflictError, match="Cannot delete a palette color that is in use"):
            await view.delete_model(cast(Any, None), 1)
    finally:
        monkeypatch.setattr(ModelView, "delete_model", original)
