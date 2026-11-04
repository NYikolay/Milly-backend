"""task series generation index

Revision ID: 0004_task_series_generation_index
Revises: 0003_task_management
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task_series_generation_index"
down_revision: str | None = "0003_task_management"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """
    Create the partial generation-eligibility index on task series.

    Purpose:
        Speed up the periodic generation scan that filters and orders
        non-deleted series by ``generated_through``.

    Notes:
        - The partial predicate mirrors the ORM metadata exactly.
    """
    op.create_index(
        "ix_task_series_generated_through_active",
        "task_series",
        ["generated_through"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """
    Drop the partial generation-eligibility index.

    Purpose:
        Revert the schema to the task-management revision.
    """
    op.drop_index(
        "ix_task_series_generated_through_active",
        table_name="task_series",
    )
