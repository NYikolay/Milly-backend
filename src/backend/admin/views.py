from __future__ import annotations

from typing import Any

from sqladmin import ModelView
from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    BooleanFilter,
    ForeignKeyFilter,
    OperationColumnFilter,
)
from sqlalchemy import func, select
from starlette.requests import Request

from backend.core.errors import ConflictError
from backend.models.auth_session import AuthSession
from backend.models.color_palette import ColorPalette
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


class ColorPaletteAdmin(ModelView, model=ColorPalette):
    """Admin view for the global color palette."""

    column_list = [
        ColorPalette.id,
        ColorPalette.slug,
        ColorPalette.name,
        ColorPalette.hex,
        ColorPalette.sort_order,
        ColorPalette.is_active,
    ]
    column_searchable_list = [ColorPalette.slug, ColorPalette.name]
    column_sortable_list = [ColorPalette.id, ColorPalette.sort_order, ColorPalette.name]
    column_filters = [
        BooleanFilter(ColorPalette.is_active),
        OperationColumnFilter(ColorPalette.slug),
    ]
    column_default_sort = (ColorPalette.sort_order, False)
    form_excluded_columns = [ColorPalette.id]

    async def on_model_change(
        self,
        data: dict[str, Any],
        model: ColorPalette,
        is_created: bool,
        request: Request,
    ) -> None:
        """
        Validate palette edits before SQLAdmin persists them.

        Purpose:
            Keep palette slugs stable and prevent the panel from leaving zero
            active colors.

        Args:
            data: Submitted field values to apply to the model.
            model: Palette color being created or updated.
            is_created: Whether the submission creates a new color.
            request: Admin request performing the change.

        Raises:
            ConflictError: If an existing slug changes or the update
                deactivates the last active color.

        Notes:
            - The active-color count excludes the model being changed and is
              read through SQLAdmin's async session factory.
        """
        if not is_created and data.get("slug", model.slug) != model.slug:
            raise ConflictError("Palette color slug is immutable")
        if not is_created and data.get("is_active") is False:
            async with self.session_maker() as session:
                result = await session.execute(
                    select(func.count())
                    .select_from(ColorPalette)
                    .where(
                        ColorPalette.is_active.is_(True),
                        ColorPalette.id != model.id,
                    )
                )
                if result.scalar_one() == 0:
                    raise ConflictError("Cannot deactivate the last active palette color")

    async def on_model_delete(self, model: ColorPalette, request: Request) -> None:
        """
        Validate palette deletions before SQLAdmin removes the color.

        Purpose:
            Prevent the panel from deleting the last active palette color.

        Args:
            model: Palette color selected for deletion.
            request: Admin request performing the deletion.

        Raises:
            ConflictError: If the color is active and no other active color
                remains.

        Notes:
            - Inactive colors are deletable unless referenced, where the
              database foreign key still enforces ``ON DELETE RESTRICT``.
        """
        if not model.is_active:
            return
        async with self.session_maker() as session:
            result = await session.execute(
                select(func.count())
                .select_from(ColorPalette)
                .where(
                    ColorPalette.is_active.is_(True),
                    ColorPalette.id != model.id,
                )
            )
            if result.scalar_one() == 0:
                raise ConflictError("Cannot delete the last active palette color")


class UserAdmin(ModelView, model=User):
    """Admin view for application users."""

    column_list = [User.id, User.created_at, User.updated_at]
    column_searchable_list = [User.id]
    column_sortable_list = [User.id, User.created_at, User.updated_at]
    column_filters = [
        OperationColumnFilter(User.id),
        OperationColumnFilter(User.created_at),
        OperationColumnFilter(User.updated_at),
    ]
    column_default_sort = (User.id, True)
    form_include_pk = True
    form_excluded_columns = [User.id, User.telegram_profile]


class TelegramProfileAdmin(ModelView, model=TelegramProfile):
    """Admin view for Telegram profiles."""

    column_list = [
        TelegramProfile.id,
        TelegramProfile.telegram_id,
        TelegramProfile.username,
        TelegramProfile.first_name,
        TelegramProfile.last_name,
        TelegramProfile.is_bot,
        TelegramProfile.is_premium,
        TelegramProfile.language_code,
    ]
    column_searchable_list = [
        TelegramProfile.telegram_id,
        TelegramProfile.username,
        TelegramProfile.first_name,
        TelegramProfile.last_name,
    ]
    column_sortable_list = [
        TelegramProfile.id,
        TelegramProfile.telegram_id,
        TelegramProfile.is_premium,
        TelegramProfile.language_code,
    ]
    column_filters = [
        BooleanFilter(TelegramProfile.is_bot),
        BooleanFilter(TelegramProfile.is_premium),
        AllUniqueStringValuesFilter(TelegramProfile.language_code),
        ForeignKeyFilter(
            TelegramProfile.user_id,
            User.id,
            foreign_model=User,
            title="User",
        ),
        OperationColumnFilter(TelegramProfile.telegram_id),
    ]
    column_default_sort = (TelegramProfile.id, True)
    form_include_pk = True
    form_excluded_columns = [TelegramProfile.id, TelegramProfile.user]


class AuthSessionAdmin(ModelView, model=AuthSession):
    """Admin view for refresh-token sessions."""

    column_list = [
        AuthSession.id,
        AuthSession.user_id,
        AuthSession.family_id,
        AuthSession.created_at,
        AuthSession.expires_at,
        AuthSession.revoked_at,
    ]
    column_details_exclude_list = [AuthSession.refresh_token_hash]
    column_searchable_list = [AuthSession.user_id, AuthSession.family_id]
    column_sortable_list = [
        AuthSession.id,
        AuthSession.created_at,
        AuthSession.expires_at,
        AuthSession.revoked_at,
    ]
    column_filters = [
        ForeignKeyFilter(
            AuthSession.user_id,
            User.id,
            foreign_model=User,
            title="User",
        ),
        OperationColumnFilter(AuthSession.family_id),
        OperationColumnFilter(AuthSession.created_at),
        OperationColumnFilter(AuthSession.expires_at),
        OperationColumnFilter(AuthSession.revoked_at),
    ]
    column_default_sort = (AuthSession.id, True)
    form_include_pk = True
    form_excluded_columns = [AuthSession.id]
