"""Apply database migrations, once, with a backup first.

Runs as its own container before anything else opens the database — see the
``migrate`` service in ``compose.pi.yaml``. Three processes share one SQLite
file and watchtower restarts them together, so migrating on application startup
would race with itself.

Handles the case the deploy will actually hit first: a database that already has
tables and has never heard of alembic. Upgrading that would try to create tables
that exist. It is stamped at the baseline instead, then upgraded from there.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlmodel import SQLModel

import soma.models  # noqa: F401  (registers tables on the metadata)
from soma.clock import now
from soma.config import ROOT, settings

log = logging.getLogger("soma.migrate")

# A Pi runs on an SD card. Unbounded backups fill it, and a full disk during a
# migration is how you lose the database you were protecting.
KEEP_BACKUPS = 5


def _backup(db: Path) -> Path | None:
    """Copy the database before touching it.

    Uses SQLite's own backup API rather than a file copy: a WAL database is two
    files plus a shared-memory index, and copying only the first produces
    something that opens cleanly and is missing the most recent writes.
    """
    if not db.exists():
        return None
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    target = db.with_name(f"{db.stem}.pre-migration-{stamp}.db")
    with sqlite3.connect(db) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    log.info("Backed up %s -> %s", db, target.name)

    old = sorted(db.parent.glob(f"{db.stem}.pre-migration-*.db"))[:-KEEP_BACKUPS]
    for path in old:
        path.unlink()
        log.info("Pruned old backup %s", path.name)
    return target


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    return cfg


def _matches_head(url: str) -> bool:
    """Whether the schema already is what the models describe.

    An unversioned database is not necessarily an *old* one. Before alembic,
    `create_all` built the current schema — so a database made that way is at
    head, and stamping it at the baseline would then try to re-run every
    migration against tables that are already correct.

    Asking the schema rather than guessing keeps this generic: no revision id
    appears here, so it stays right as revisions are added.
    """
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            return not compare_metadata(ctx, SQLModel.metadata)
    finally:
        engine.dispose()


def _baseline_revision(cfg: Config) -> str:
    """The first revision in the chain, found rather than hardcoded."""
    script = ScriptDirectory.from_config(cfg)
    bases = script.get_bases()
    if len(bases) != 1:
        raise RuntimeError(f"expected exactly one base revision, found {bases}")
    return bases[0]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = Path(settings.db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = _alembic_config()

    engine = create_engine(settings.db_url)
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    engine.dispose()

    if tables:
        _backup(db)

    if tables and "alembic_version" not in tables:
        if _matches_head(settings.db_url):
            log.info(
                "Existing database already matches the models; stamping head "
                "rather than replaying migrations over a correct schema."
            )
            command.stamp(cfg, "head")
        else:
            base = _baseline_revision(cfg)
            log.info(
                "Existing database with no alembic_version; stamping baseline %s "
                "rather than re-creating %s existing table(s).",
                base,
                len(tables),
            )
            command.stamp(cfg, base)

    command.upgrade(cfg, "head")
    log.info("Migrations up to date.")


if __name__ == "__main__":
    sys.exit(main())
