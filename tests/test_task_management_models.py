from __future__ import annotations

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    SmallInteger,
    String,
    UniqueConstraint,
)

import backend.models  # noqa: F401
from backend.db.base import Base


def test_task_management_tables_are_registered() -> None:
    assert {
        "color_palette",
        "projects",
        "labels",
        "task_series",
        "tasks",
        "task_labels",
        "series_labels",
        "task_reminders",
        "reminder_deliveries",
    } <= set(Base.metadata.tables)


def test_user_timezone_defaults_to_utc() -> None:
    table = Base.metadata.tables["users"]
    assert isinstance(table.c.timezone.type, String)
    assert table.c.timezone.type.length == 64
    assert table.c.timezone.nullable is False
    assert table.c.timezone.server_default is not None


def test_task_management_primary_keys_use_identity() -> None:
    for table_name in (
        "color_palette",
        "projects",
        "labels",
        "task_series",
        "tasks",
        "task_reminders",
        "reminder_deliveries",
    ):
        column = Base.metadata.tables[table_name].c.id
        assert isinstance(column.type, BigInteger)
        assert isinstance(column.identity, Identity)


def test_color_palette_columns_and_indexes() -> None:
    table = Base.metadata.tables["color_palette"]
    assert isinstance(table.c.hex.type, CHAR)
    assert table.c.hex.type.length == 7
    assert table.c.slug.unique is True
    index_names = {index.name for index in table.indexes}
    assert "ix_color_palette_is_active_sort_order_id" in index_names


def test_tasks_have_timestamptz_due_and_completion() -> None:
    table = Base.metadata.tables["tasks"]
    assert isinstance(table.c.due_at.type, DateTime)
    assert table.c.due_at.type.timezone is True
    assert isinstance(table.c.completed_at.type, DateTime)
    assert table.c.completed_at.type.timezone is True


def test_composite_foreign_keys_include_user_id() -> None:
    tasks = Base.metadata.tables["tasks"]
    targets = {
        tuple(column.name for column in constraint.columns): constraint.elements[0].target_fullname
        for constraint in tasks.foreign_key_constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert targets[("project_id", "user_id")] == "projects.id"
    assert targets[("series_id", "user_id")] == "task_series.id"


def test_partial_unique_indexes_are_declared() -> None:
    tasks = Base.metadata.tables["tasks"]
    series_slot = next(
        index for index in tasks.indexes if index.name == "uq_tasks_series_id_due_at"
    )
    assert series_slot.unique is True
    assert series_slot.dialect_options["postgresql"]["where"] is not None

    reminders = Base.metadata.tables["task_reminders"]
    kind_indexes = {
        index.name
        for index in reminders.indexes
        if index.name in {"uq_task_reminders_task_id_kind", "uq_task_reminders_series_id_kind"}
    }
    assert kind_indexes == {
        "uq_task_reminders_task_id_kind",
        "uq_task_reminders_series_id_kind",
    }


def test_delivery_has_idempotency_key() -> None:
    table = Base.metadata.tables["reminder_deliveries"]
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("reminder_id", "scheduled_for") in unique_columns


def test_task_series_interval_days_column_and_checks() -> None:
    table = Base.metadata.tables["task_series"]
    assert isinstance(table.c.interval_days.type, SmallInteger)
    assert table.c.interval_days.nullable is False
    assert table.c.interval_days.server_default is not None
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "interval_days >= 1" in checks["ck_task_series_interval_days_positive"]
    assert "frequency = 'daily'" in checks["ck_task_series_frequency_interval"]
    assert "frequency = 'periodic'" in checks["ck_task_series_frequency_interval"]
    assert "ck_task_series_frequency_daily" not in checks


def test_check_constraints_are_declared() -> None:
    expected_names = {
        "color_palette": {
            "ck_color_palette_slug_format",
            "ck_color_palette_hex_format",
        },
        "task_series": {
            "ck_task_series_frequency",
            "ck_task_series_interval_days_positive",
            "ck_task_series_frequency_interval",
            "ck_task_series_ends_on_after_starts_on",
            "ck_task_series_priority",
        },
        "tasks": {"ck_tasks_priority"},
        "task_reminders": {
            "ck_task_reminders_one_owner",
            "ck_task_reminders_kind",
            "ck_task_reminders_offset_minutes_non_negative",
            "ck_task_reminders_interval_days_positive",
        },
        "reminder_deliveries": {
            "ck_reminder_deliveries_attempts_non_negative",
            "ck_reminder_deliveries_status",
        },
    }
    for table_name, names in expected_names.items():
        table = Base.metadata.tables[table_name]
        actual_names = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert names <= actual_names
