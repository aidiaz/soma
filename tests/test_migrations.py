"""Migrations and models must describe the same schema.

Tests build their database with ``create_all`` because it is fast. Production
builds it with alembic. That is two definitions of one schema, and they drift
the moment nobody checks — the failure being a migration that works in CI and
leaves the Pi with a column the code expects and the table lacks.
"""

from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

import soma.models  # noqa: F401  (registers tables on the metadata)
from soma.config import ROOT


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(_config(url), "head")
    return url


def test_migrations_produce_the_schema_the_models_describe(migrated):
    """The drift guard. A model change without a revision fails here."""
    engine = create_engine(migrated)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, SQLModel.metadata)
    engine.dispose()
    assert diffs == [], (
        "models and migrations disagree. Generate a revision:\n"
        "  uv run alembic revision --autogenerate -m '<what changed>'\n"
        f"differences: {diffs}"
    )


def test_every_model_table_exists_after_upgrade(migrated):
    engine = create_engine(migrated)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    engine.dispose()
    missing = set(SQLModel.metadata.tables) - tables
    assert not missing, f"upgrade did not create: {sorted(missing)}"


def test_an_existing_unversioned_database_is_stamped_not_recreated(tmp_path, monkeypatch):
    """The case the real deploy hits: tables already there, alembic never run.

    Upgrading such a database would try to create tables that exist. It must be
    stamped at the baseline instead, and the rows must survive.
    """
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)  # the old world: create_all, no versioning
    engine.dispose()
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO body (date, weight_kg) VALUES ('2026-08-24', 71.4)")

    import soma.config as cfg
    from soma import migrate

    monkeypatch.setattr(cfg.settings, "db_path", db)
    migrate.main()

    with sqlite3.connect(db) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        rows = conn.execute("SELECT weight_kg FROM body").fetchall()
    assert version is not None, "database was not stamped"
    assert rows == [(71.4,)], "existing data did not survive the migration"


def test_a_backup_is_taken_before_migrating(tmp_path, monkeypatch):
    db = tmp_path / "b.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    import soma.config as cfg
    from soma import migrate

    monkeypatch.setattr(cfg.settings, "db_path", db)
    migrate.main()

    backups = list(tmp_path.glob("b.pre-migration-*.db"))
    assert len(backups) == 1, "the only copy of manually entered data must be copied first"
    assert sqlite3.connect(backups[0]).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
