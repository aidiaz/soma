"""Alembic environment.

Two settings here are load-bearing rather than boilerplate:

``render_as_batch`` — SQLite cannot ALTER most things. Changing or dropping a
column means creating a new table, copying every row, and swapping them. Batch
mode does that; without it a migration that looks fine locally fails on the Pi.

``target_metadata`` — taken from SQLModel, so ``--autogenerate`` compares
against the models rather than a hand-maintained copy. A test asserts the two
agree, because two definitions of a schema drift the moment nobody is checking.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import soma.models  # noqa: F401  (registers every table on the metadata)
from soma.config import settings

config = context.config

# Settings are the default, not an override. A caller that set a URL — the
# migrate entry point, or a test against a throwaway file — means it, and
# clobbering it here silently migrates the wrong database.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.db_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
