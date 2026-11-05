from __future__ import annotations

import importlib.util
import io
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from backend.admin.views import ColorPaletteAdmin
from backend.db.base import Base
from backend.models.color_palette import ColorPalette


def _load_migration() -> ModuleType:
    """Load the task-management migration module from disk."""
    path = Path("alembic/versions/0003_task_management.py")
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_generation_index_migration() -> ModuleType:
    """Load the series generation index migration module from disk."""
    path = Path("alembic/versions/0004_task_series_generation_index.py")
    spec = importlib.util.spec_from_file_location("migration_0004", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_periodic_migration() -> ModuleType:
    path = Path("alembic/versions/0005_periodic_task_series.py")
    spec = importlib.util.spec_from_file_location("migration_0005", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_periodic_migration_chain() -> None:
    migration = _load_periodic_migration()
    assert migration.revision == "0005_periodic_task_series"
    assert migration.down_revision == "0004_task_series_generation_index"


def test_periodic_migration_upgrade_and_downgrade_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_periodic_migration()
    calls: list[tuple[object, ...]] = []

    class FakeOp:
        def add_column(self, *args: object, **kwargs: object) -> None:
            calls.append(("add_column", args, kwargs))

        def drop_constraint(self, *args: object, **kwargs: object) -> None:
            calls.append(("drop_constraint", args, kwargs))

        def create_check_constraint(self, *args: object, **kwargs: object) -> None:
            calls.append(("create_check_constraint", args, kwargs))

        def drop_column(self, *args: object, **kwargs: object) -> None:
            calls.append(("drop_column", args, kwargs))

        def execute(self, *args: object, **kwargs: object) -> None:
            calls.append(("execute", args, kwargs))

    monkeypatch.setattr(migration, "op", FakeOp())
    migration.upgrade()
    assert any(call[0] == "add_column" for call in calls)
    assert any(call[0] == "create_check_constraint" for call in calls)
    calls.clear()
    migration.downgrade()
    assert any(call[0] == "drop_column" for call in calls)
    assert any(call[0] == "execute" for call in calls)


def _render_migration_sql(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[[], None],
) -> list[str]:
    """Render one migration step offline and return the emitted SQL statements."""
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "target_metadata": Base.metadata,
            "as_sql": True,
            "output_buffer": output,
        },
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    run()
    return [statement.strip() for statement in output.getvalue().split(";") if statement.strip()]


def test_periodic_migration_renders_exact_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _load_periodic_migration()

    upgrade_statements = _render_migration_sql(migration, monkeypatch, migration.upgrade)
    assert upgrade_statements == [
        "ALTER TABLE task_series ADD COLUMN interval_days SMALLINT DEFAULT 1 NOT NULL",
        "ALTER TABLE task_series DROP CONSTRAINT ck_task_series_frequency_daily",
        "ALTER TABLE task_series DROP CONSTRAINT ck_task_series_frequency",
        (
            "ALTER TABLE task_series ADD CONSTRAINT ck_task_series_frequency"
            " CHECK (frequency IN ('daily', 'periodic'))"
        ),
        (
            "ALTER TABLE task_series ADD CONSTRAINT ck_task_series_interval_days_positive"
            " CHECK (interval_days >= 1)"
        ),
        (
            "ALTER TABLE task_series ADD CONSTRAINT ck_task_series_frequency_interval"
            " CHECK ((frequency = 'daily' AND interval_days = 1)"
            " OR (frequency = 'periodic' AND interval_days >= 2))"
        ),
    ]

    downgrade_statements = _render_migration_sql(migration, monkeypatch, migration.downgrade)
    assert downgrade_statements == [
        "UPDATE task_series SET frequency = 'daily', interval_days = 1"
        " WHERE frequency = 'periodic'",
        "ALTER TABLE task_series DROP CONSTRAINT ck_task_series_frequency_interval",
        "ALTER TABLE task_series DROP CONSTRAINT ck_task_series_interval_days_positive",
        "ALTER TABLE task_series DROP CONSTRAINT ck_task_series_frequency",
        "ALTER TABLE task_series DROP COLUMN interval_days",
        (
            "ALTER TABLE task_series ADD CONSTRAINT ck_task_series_frequency_daily"
            " CHECK (frequency = 'daily')"
        ),
        (
            "ALTER TABLE task_series ADD CONSTRAINT ck_task_series_frequency"
            " CHECK (frequency IN ('daily'))"
        ),
    ]


def test_generation_index_migration_chain_and_model_index() -> None:
    """Chain the index migration and mirror its predicate in the ORM metadata."""
    migration = _load_generation_index_migration()
    assert migration.revision == "0004_task_series_generation_index"
    assert migration.down_revision == "0003_task_management"

    table = Base.metadata.tables["task_series"]
    index = next(
        index for index in table.indexes if index.name == "ix_task_series_generated_through_active"
    )
    assert [column.name for column in index.columns] == ["generated_through"]
    assert index.dialect_options["postgresql"]["where"] is not None


def test_migration_revision_chain() -> None:
    """Chain the task-management migration after Telegram auth."""
    migration = _load_migration()
    assert migration.revision == "0003_task_management"
    assert migration.down_revision == "0002_telegram_auth"


def test_migration_seeds_fifteen_unique_colors() -> None:
    """Seed the fifteen default palette colors with unique slugs."""
    migration = _load_migration()
    assert len(migration.SEED_COLORS) == 15
    assert [row["slug"] for row in migration.SEED_COLORS][0] == "grape"
    assert len({row["slug"] for row in migration.SEED_COLORS}) == 15
    for row in migration.SEED_COLORS:
        assert len(row["hex"]) == 7 and row["hex"].startswith("#")


def test_palette_admin_is_bound_to_model() -> None:
    """Bind the palette admin view to its ORM model and identity."""
    assert ColorPaletteAdmin.model is ColorPalette
    assert ColorPaletteAdmin.identity == "color-palette"


def test_migration_seed_matches_spec_table_exactly() -> None:
    """Seed exactly the fifteen palette tuples defined by the specification."""
    migration = _load_migration()
    actual = [
        (row["sort_order"], row["slug"], row["name"], row["hex"]) for row in migration.SEED_COLORS
    ]
    assert actual == [
        (1, "grape", "Виноградный", "#BB6BD9"),
        (2, "blue", "Синий", "#2F80ED"),
        (3, "sky", "Голубой", "#56CCF2"),
        (4, "teal", "Бирюзовый", "#2D9CDB"),
        (5, "green", "Зелёный", "#27AE60"),
        (6, "mint", "Мятный", "#6FCF97"),
        (7, "lime", "Лаймовый", "#B4D455"),
        (8, "yellow", "Жёлтый", "#F2C94C"),
        (9, "orange", "Оранжевый", "#F2994A"),
        (10, "red", "Красный", "#EB5757"),
        (11, "coral", "Коралловый", "#F26B5B"),
        (12, "pink", "Розовый", "#F178B6"),
        (13, "purple", "Фиолетовый", "#9B51E0"),
        (14, "indigo", "Индиго", "#5B6CFF"),
        (15, "gray", "Серый", "#828282"),
    ]
