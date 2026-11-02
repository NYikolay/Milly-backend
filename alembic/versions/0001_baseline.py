"""baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-21
"""

from __future__ import annotations

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create the baseline revision."""


def downgrade() -> None:
    """Drop the baseline revision."""
