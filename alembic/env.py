"""Alembic environment configuration for async migrations."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import backend.models  # noqa: F401
from backend.core.config import get_settings
from backend.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in offline mode and emit SQL without a database connection.

    Purpose:
        Supports ``alembic upgrade head --sql`` and similar commands that render
        migration SQL for a target database.

    Notes:
        - Uses ``DIRECT_DATABASE_URL`` so generated SQL targets the direct database.
        - No engine or connection is created in this mode.
    """
    settings = get_settings()
    context.configure(
        url=settings.direct_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Run migrations using an established synchronous connection.

    Purpose:
        Bridges Alembic's synchronous migration runner to the async engine via
        ``AsyncConnection.run_sync``.

    Args:
        connection: Synchronous connection bound to the migration transaction.

    Notes:
        - Commits are managed by Alembic's migration transaction context.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Create an async engine and apply migrations through a synchronous bridge.

    Purpose:
        Runs online migrations against ``DIRECT_DATABASE_URL`` with an async
        SQLAlchemy engine.

    Notes:
        - Uses ``NullPool`` so connections are not reused across migrations.
        - The engine is disposed after the migration run completes.
    """
    settings = get_settings()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.direct_database_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """
    Run migrations in online mode against the direct database URL.

    Purpose:
        Entry point for Alembic commands that apply migrations to a database.

    Notes:
        - Creates a dedicated event loop with ``asyncio.run``.
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
