"""task management

Revision ID: 0003_task_management
Revises: 0002_telegram_auth
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_task_management"
down_revision: str | None = "0002_telegram_auth"
branch_labels: str | None = None
depends_on: str | None = None

SEED_COLORS: tuple[dict[str, str | int], ...] = (
    {"slug": "grape", "name": "Виноградный", "hex": "#BB6BD9", "sort_order": 1},
    {"slug": "blue", "name": "Синий", "hex": "#2F80ED", "sort_order": 2},
    {"slug": "sky", "name": "Голубой", "hex": "#56CCF2", "sort_order": 3},
    {"slug": "teal", "name": "Бирюзовый", "hex": "#2D9CDB", "sort_order": 4},
    {"slug": "green", "name": "Зелёный", "hex": "#27AE60", "sort_order": 5},
    {"slug": "mint", "name": "Мятный", "hex": "#6FCF97", "sort_order": 6},
    {"slug": "lime", "name": "Лаймовый", "hex": "#B4D455", "sort_order": 7},
    {"slug": "yellow", "name": "Жёлтый", "hex": "#F2C94C", "sort_order": 8},
    {"slug": "orange", "name": "Оранжевый", "hex": "#F2994A", "sort_order": 9},
    {"slug": "red", "name": "Красный", "hex": "#EB5757", "sort_order": 10},
    {"slug": "coral", "name": "Коралловый", "hex": "#F26B5B", "sort_order": 11},
    {"slug": "pink", "name": "Розовый", "hex": "#F178B6", "sort_order": 12},
    {"slug": "purple", "name": "Фиолетовый", "hex": "#9B51E0", "sort_order": 13},
    {"slug": "indigo", "name": "Индиго", "hex": "#5B6CFF", "sort_order": 14},
    {"slug": "gray", "name": "Серый", "hex": "#828282", "sort_order": 15},
)

_PRIORITY_CHECK = (
    "priority IN ('urgent_important', 'important_not_urgent', 'urgent_not_important', 'neither')"
)
_STATUS_CHECK = "status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')"
_KIND_CHECK = "kind IN ('before_due', 'scheduled')"


def upgrade() -> None:
    """
    Create the task-management schema and seed the default palette.

    Purpose:
        Add the global color palette, the user timezone column, projects,
        labels, recurring series, tasks, label assignments, reminders, and
        reminder deliveries on top of the Telegram auth revision.

    Notes:
        - Tables are created in dependency order, starting with the palette.
        - The fifteen default colors are inserted before any table can
          reference ``color_palette``.
        - Partial unique and lookup indexes mirror the ORM metadata exactly.
    """
    op.create_table(
        "color_palette",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("hex", sa.String(length=7), nullable=False),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9_-]+$'",
            name="slug_format",
        ),
        sa.CheckConstraint(
            "hex ~ '^#[0-9A-Fa-f]{6}$'",
            name="hex_format",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_color_palette"),
        sa.UniqueConstraint("slug", name="uq_color_palette_slug"),
    )
    op.create_index(
        "ix_color_palette_is_active_sort_order_id",
        "color_palette",
        ["is_active", "sort_order", "id"],
    )
    palette_table = sa.table(
        "color_palette",
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("hex", sa.String),
        sa.column("sort_order", sa.SmallInteger),
    )
    op.bulk_insert(palette_table, list(SEED_COLORS))
    op.add_column(
        "users",
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'UTC'"),
            nullable=False,
        ),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color_id", sa.BigInteger(), nullable=False),
        sa.Column("icon", sa.String(length=32), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["color_id"],
            ["color_palette.id"],
            name="fk_projects_color_id_color_palette",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_projects_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("id", "user_id", name="uq_projects_id_user_id"),
    )
    op.create_index(
        "uq_projects_user_id_lower_name_active",
        "projects",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND archived_at IS NULL"),
    )
    op.create_index(
        "ix_projects_user_id_archived_at_active",
        "projects",
        ["user_id", "archived_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_projects_color_id", "projects", ["color_id"])
    op.create_table(
        "labels",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("color_id", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["color_id"],
            ["color_palette.id"],
            name="fk_labels_color_id_color_palette",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_labels_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_labels"),
        sa.UniqueConstraint("id", "user_id", name="uq_labels_id_user_id"),
    )
    op.create_index(
        "uq_labels_user_id_lower_name_active",
        "labels",
        ["user_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_labels_user_id_active",
        "labels",
        ["user_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_labels_color_id", "labels", ["color_id"])
    op.create_table(
        "task_series",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "priority",
            sa.String(length=32),
            server_default=sa.text("'neither'"),
            nullable=False,
        ),
        sa.Column(
            "frequency",
            sa.String(length=16),
            server_default=sa.text("'daily'"),
            nullable=False,
        ),
        sa.Column("time_of_day", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("generated_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reminders_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "frequency = 'daily'",
            name="frequency_daily",
        ),
        sa.CheckConstraint(
            "frequency IN ('daily')",
            name="frequency",
        ),
        sa.CheckConstraint(
            "ends_on IS NULL OR ends_on >= starts_on",
            name="ends_on_after_starts_on",
        ),
        sa.CheckConstraint(_PRIORITY_CHECK, name="priority"),
        sa.ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            name="fk_task_series_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_task_series_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_series"),
        sa.UniqueConstraint("id", "user_id", name="uq_task_series_id_user_id"),
    )
    op.create_index(
        "ix_task_series_user_id_generated_through_active",
        "task_series",
        ["user_id", "generated_through"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("series_id", sa.BigInteger(), nullable=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "priority",
            sa.String(length=32),
            server_default=sa.text("'neither'"),
            nullable=False,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reminders_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_batch_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_PRIORITY_CHECK, name="priority"),
        sa.ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            name="fk_tasks_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["series_id", "user_id"],
            ["task_series.id", "task_series.user_id"],
            name="fk_tasks_series_id_task_series",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_tasks_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("id", "user_id", name="uq_tasks_id_user_id"),
    )
    op.create_index(
        "uq_tasks_series_id_due_at",
        "tasks",
        ["series_id", "due_at"],
        unique=True,
        postgresql_where=sa.text("series_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_user_id_due_at_id",
        "tasks",
        ["user_id", "due_at", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_user_id_completed_at",
        "tasks",
        ["user_id", "completed_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_user_id_project_id_due_at",
        "tasks",
        ["user_id", "project_id", "due_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_user_id_priority_due_at",
        "tasks",
        ["user_id", "priority", "due_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_user_id_series_id",
        "tasks",
        ["user_id", "series_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_project_id_completed_at",
        "tasks",
        ["project_id", "completed_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "task_labels",
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("label_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            name="fk_task_labels_label_id_labels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "user_id"],
            ["tasks.id", "tasks.user_id"],
            name="fk_task_labels_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "label_id", name="pk_task_labels"),
    )
    op.create_index(
        "ix_task_labels_label_id_task_id",
        "task_labels",
        ["label_id", "task_id"],
    )
    op.create_index(
        "ix_task_labels_user_id_label_id",
        "task_labels",
        ["user_id", "label_id"],
    )
    op.create_table(
        "series_labels",
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("label_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            name="fk_series_labels_label_id_labels",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id", "user_id"],
            ["task_series.id", "task_series.user_id"],
            name="fk_series_labels_series_id_task_series",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("series_id", "label_id", name="pk_series_labels"),
    )
    op.create_index(
        "ix_series_labels_label_id_series_id",
        "series_labels",
        ["label_id", "series_id"],
    )
    op.create_index(
        "ix_series_labels_user_id_label_id",
        "series_labels",
        ["user_id", "label_id"],
    )
    op.create_table(
        "task_reminders",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("series_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=True),
        sa.Column("interval_days", sa.SmallInteger(), nullable=True),
        sa.Column("time_of_day", sa.Time(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(CASE WHEN task_id IS NULL THEN 0 ELSE 1 END)"
            " + (CASE WHEN series_id IS NULL THEN 0 ELSE 1 END) = 1",
            name="one_owner",
        ),
        sa.CheckConstraint(_KIND_CHECK, name="kind"),
        sa.CheckConstraint(
            "offset_minutes >= 0",
            name="offset_minutes_non_negative",
        ),
        sa.CheckConstraint(
            "interval_days > 0",
            name="interval_days_positive",
        ),
        sa.ForeignKeyConstraint(
            ["series_id", "user_id"],
            ["task_series.id", "task_series.user_id"],
            name="fk_task_reminders_series_id_task_series",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "user_id"],
            ["tasks.id", "tasks.user_id"],
            name="fk_task_reminders_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_task_reminders_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_reminders"),
        sa.UniqueConstraint("id", "user_id", name="uq_task_reminders_id_user_id"),
    )
    op.create_index(
        "uq_task_reminders_task_id_kind",
        "task_reminders",
        ["task_id", "kind"],
        unique=True,
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )
    op.create_index(
        "uq_task_reminders_series_id_kind",
        "task_reminders",
        ["series_id", "kind"],
        unique=True,
        postgresql_where=sa.text("series_id IS NOT NULL"),
    )
    op.create_index(
        "ix_task_reminders_next_run_at",
        "task_reminders",
        ["next_run_at"],
        postgresql_where=sa.text("enabled AND next_run_at IS NOT NULL"),
    )
    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("reminder_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="attempts_non_negative",
        ),
        sa.CheckConstraint(_STATUS_CHECK, name="status"),
        sa.ForeignKeyConstraint(
            ["reminder_id", "user_id"],
            ["task_reminders.id", "task_reminders.user_id"],
            name="fk_reminder_deliveries_reminder_id_task_reminders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_reminder_deliveries_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reminder_deliveries"),
        sa.UniqueConstraint(
            "reminder_id",
            "scheduled_for",
            name="uq_reminder_deliveries_reminder_id_scheduled_for",
        ),
    )
    op.create_index(
        "ix_reminder_deliveries_status_next_attempt_at",
        "reminder_deliveries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_reminder_deliveries_user_id_scheduled_for",
        "reminder_deliveries",
        ["user_id", "scheduled_for"],
    )


def downgrade() -> None:
    """
    Drop the task-management schema and the user timezone column.

    Purpose:
        Revert the task-management revision back to Telegram auth while
        removing dependent indexes and tables in reverse dependency order.

    Notes:
        - ``color_palette`` is dropped last because projects and labels
          reference it.
        - The seeded palette rows disappear with their table.
    """
    op.drop_index(
        "ix_reminder_deliveries_user_id_scheduled_for",
        table_name="reminder_deliveries",
    )
    op.drop_index(
        "ix_reminder_deliveries_status_next_attempt_at",
        table_name="reminder_deliveries",
    )
    op.drop_table("reminder_deliveries")
    op.drop_index("ix_task_reminders_next_run_at", table_name="task_reminders")
    op.drop_index(
        "uq_task_reminders_series_id_kind",
        table_name="task_reminders",
    )
    op.drop_index("uq_task_reminders_task_id_kind", table_name="task_reminders")
    op.drop_table("task_reminders")
    op.drop_index(
        "ix_series_labels_user_id_label_id",
        table_name="series_labels",
    )
    op.drop_index(
        "ix_series_labels_label_id_series_id",
        table_name="series_labels",
    )
    op.drop_table("series_labels")
    op.drop_index(
        "ix_task_labels_user_id_label_id",
        table_name="task_labels",
    )
    op.drop_index(
        "ix_task_labels_label_id_task_id",
        table_name="task_labels",
    )
    op.drop_table("task_labels")
    op.drop_index(
        "ix_tasks_project_id_completed_at",
        table_name="tasks",
    )
    op.drop_index("ix_tasks_user_id_series_id", table_name="tasks")
    op.drop_index("ix_tasks_user_id_priority_due_at", table_name="tasks")
    op.drop_index("ix_tasks_user_id_project_id_due_at", table_name="tasks")
    op.drop_index("ix_tasks_user_id_completed_at", table_name="tasks")
    op.drop_index("ix_tasks_user_id_due_at_id", table_name="tasks")
    op.drop_index("uq_tasks_series_id_due_at", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index(
        "ix_task_series_user_id_generated_through_active",
        table_name="task_series",
    )
    op.drop_table("task_series")
    op.drop_index("ix_labels_color_id", table_name="labels")
    op.drop_index("ix_labels_user_id_active", table_name="labels")
    op.drop_index("uq_labels_user_id_lower_name_active", table_name="labels")
    op.drop_table("labels")
    op.drop_index("ix_projects_color_id", table_name="projects")
    op.drop_index(
        "ix_projects_user_id_archived_at_active",
        table_name="projects",
    )
    op.drop_index(
        "uq_projects_user_id_lower_name_active",
        table_name="projects",
    )
    op.drop_table("projects")
    op.drop_column("users", "timezone")
    op.drop_index(
        "ix_color_palette_is_active_sort_order_id",
        table_name="color_palette",
    )
    op.drop_table("color_palette")
