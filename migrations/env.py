"""Alembic environment.

Migrations run under a PostgreSQL advisory lock (NFR-D-03) so that N replicas
starting at once serialise rather than racing. The one-shot ``migrate``
container makes this rare, but ``docker compose up`` invoked twice, or a
Kubernetes job retried, both hit it.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# Import every module that owns tables so `Base.metadata` is complete.
# A module missing from this list silently drops out of autogenerate.
import cairn.catalog.models
import cairn.identity.models
import cairn.modelgw.models
import cairn.platform.models
import cairn.tasks.models  # noqa: F401
from cairn.core.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

MIGRATION_LOCK_KEY = 0x0CA1_1000


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    from cairn.core.config import get_settings

    return get_settings().database_url.replace("+asyncpg", "")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
            )
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
