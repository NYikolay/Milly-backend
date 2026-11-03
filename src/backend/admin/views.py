from __future__ import annotations

from sqladmin import ModelView
from sqladmin.filters import (
    AllUniqueStringValuesFilter,
    BooleanFilter,
    ForeignKeyFilter,
    OperationColumnFilter,
)

from backend.models.auth_session import AuthSession
from backend.models.telegram_profile import TelegramProfile
from backend.models.user import User


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
