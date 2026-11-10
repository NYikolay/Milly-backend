from collections.abc import Iterable
from dataclasses import fields
from typing import Any, cast

import pytest
from sqlalchemy import inspect

from backend.domain.entities import (
    AuthSession,
    ColorPalette,
    Label,
    Project,
    ReminderDelivery,
    Task,
    TaskReminder,
    TaskSeries,
    TelegramProfile,
    User,
)
from backend.models.auth_session import AuthSession as AuthSessionRow
from backend.models.color_palette import ColorPalette as ColorPaletteRow
from backend.models.label import Label as LabelRow
from backend.models.project import Project as ProjectRow
from backend.models.reminder_delivery import ReminderDelivery as ReminderDeliveryRow
from backend.models.task import Task as TaskRow
from backend.models.task_reminder import TaskReminder as TaskReminderRow
from backend.models.task_series import TaskSeries as TaskSeriesRow
from backend.models.telegram_profile import TelegramProfile as TelegramProfileRow
from backend.models.user import User as UserRow


@pytest.mark.parametrize(
    ("entity", "row"),
    [
        (User, UserRow),
        (TelegramProfile, TelegramProfileRow),
        (AuthSession, AuthSessionRow),
        (ColorPalette, ColorPaletteRow),
        (Label, LabelRow),
        (Project, ProjectRow),
        (TaskSeries, TaskSeriesRow),
        (Task, TaskRow),
        (TaskReminder, TaskReminderRow),
        (ReminderDelivery, ReminderDeliveryRow),
    ],
)
def test_domain_entity_has_every_orm_column(entity: type, row: type) -> None:
    """Every persisted column has a matching domain field."""
    domain_fields = {item.name for item in fields(entity)}
    columns = cast(Iterable[Any], inspect(row).columns)
    column_fields = {column.key for column in columns}
    assert column_fields <= domain_fields
