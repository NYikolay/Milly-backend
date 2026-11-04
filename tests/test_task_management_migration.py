from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from backend.admin.views import ColorPaletteAdmin
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
