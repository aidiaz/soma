"""SQLite engine + session helpers.

The sync worker and the MCP server are separate containers sharing one database
file. SQLite's default rollback journal takes an exclusive lock for the whole of
a write, so a nightly sync would block every tool call for its duration. WAL
lets readers and one writer proceed together; ``busy_timeout`` turns the
remaining contention into a short wait instead of an immediate
``database is locked``.

The engine is built lazily so tests can repoint ``settings.db_path`` and call
:func:`reset_engine`.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from soma.config import settings

# Wait this long for a competing writer before raising "database is locked".
BUSY_TIMEOUT_MS = 5_000

_engine: Engine | None = None


def connect(dbapi_connection, connection_record) -> None:
    """Apply the PRAGMAs every new connection needs.

    ``journal_mode`` is a property of the file and persists, but re-stating it is
    harmless and means a fresh database gets WAL on its very first connection.
    ``busy_timeout`` and ``synchronous`` are per-connection and must be set here
    every time. ``synchronous=NORMAL`` is the documented safe pairing with WAL:
    it can only lose the last transactions on a power cut, never corrupt the
    file, and this is regenerable Garmin data.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
        event.listen(_engine, "connect", connect)
    return _engine


def reset_engine() -> None:
    """Drop the cached engine so the next call re-reads ``settings.db_path``."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    """Create any missing tables. **Not the schema authority.**

    Alembic owns the schema in every deployment: the ``migrate`` service runs
    ``soma-migrate`` to completion before anything else opens the database. This
    remains because tests build a throwaway database per test and running the
    full revision chain for each would be slow for no benefit.

    That is two definitions of one schema, which is a real hazard —
    ``tests/test_migrations.py`` asserts autogenerate finds no difference
    between them, so a model changed without a revision fails CI rather than
    the Pi.

    It cannot alter or drop anything, which is the whole reason alembic exists
    here. Adding a column to a model and relying on this to apply it will work
    on a fresh database and silently do nothing on yours.
    """
    import soma.models  # noqa: F401  (register tables on the metadata)

    SQLModel.metadata.create_all(get_engine())


def get_session() -> Session:
    return Session(get_engine())
