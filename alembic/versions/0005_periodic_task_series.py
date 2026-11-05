"""periodic task series

Revision ID: 0005_periodic_task_series
Revises: 0004_task_series_generation_index
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_periodic_task_series"
down_revision: str | None = "0004_task_series_generation_index"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """
    Add the series interval column and periodic-aware frequency checks.

    Purpose:
        Store the day interval on task series and allow both daily and
        periodic frequencies while keeping their interval rules consistent.

    Notes:
        - The column is backfilled with ``1`` through its server default.
        - The daily-only check is replaced by the frequency/interval pair.
    """
    op.add_column(
        "task_series",
        sa.Column(
            "interval_days",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.drop_constraint(
        "ck_task_series_frequency_daily",
        "task_series",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_series_frequency",
        "task_series",
        type_="check",
    )
    op.create_check_constraint(
        "frequency",
        "task_series",
        "frequency IN ('daily', 'periodic')",
    )
    op.create_check_constraint(
        "interval_days_positive",
        "task_series",
        "interval_days >= 1",
    )
    op.create_check_constraint(
        "frequency_interval",
        "task_series",
        "(frequency = 'daily' AND interval_days = 1)"
        " OR (frequency = 'periodic' AND interval_days >= 2)",
    )


def downgrade() -> None:
    """
    Drop the series interval column and restore daily-only frequency checks.

    Purpose:
        Revert the schema to the generation-index revision while keeping
        existing rows valid under the old daily-only constraints.

    Notes:
        - Destructive: periodic rows are normalized to daily with interval
          ``1`` because the previous schema cannot represent periodic series.
    """
    op.execute(
        "UPDATE task_series SET frequency = 'daily', interval_days = 1 WHERE frequency = 'periodic'"
    )
    op.drop_constraint(
        "ck_task_series_frequency_interval",
        "task_series",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_series_interval_days_positive",
        "task_series",
        type_="check",
    )
    op.drop_constraint(
        "ck_task_series_frequency",
        "task_series",
        type_="check",
    )
    op.drop_column("task_series", "interval_days")
    op.create_check_constraint(
        "frequency_daily",
        "task_series",
        "frequency = 'daily'",
    )
    op.create_check_constraint(
        "frequency",
        "task_series",
        "frequency IN ('daily')",
    )
