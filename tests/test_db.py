"""The PRAGMAs that let the sync worker and the server share one file."""

from __future__ import annotations

from sqlalchemy import text

from traindb.db import BUSY_TIMEOUT_MS, get_engine, get_session, init_db, reset_engine
from traindb.models import DailyHealth


def _pragma(name: str):
    with get_session() as session:
        return session.exec(text(f"PRAGMA {name}")).one()[0]


def test_journal_mode_is_wal(db):
    # Rollback journal takes an exclusive lock for a whole write, so a nightly
    # sync would block every tool call for its duration.
    assert str(_pragma("journal_mode")).lower() == "wal"


def test_busy_timeout_is_set(db):
    assert int(_pragma("busy_timeout")) == BUSY_TIMEOUT_MS


def test_synchronous_is_normal(db):
    # 1 == NORMAL, the documented safe pairing with WAL.
    assert int(_pragma("synchronous")) == 1


def test_get_engine_caches(db):
    assert get_engine() is get_engine()


def test_reset_engine_rebuilds(db):
    first = get_engine()
    reset_engine()
    assert get_engine() is not first


def test_reset_engine_picks_up_a_new_path(tmp_path, monkeypatch):
    from traindb.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "one.db")
    reset_engine()
    init_db()
    assert "one.db" in str(get_engine().url)

    monkeypatch.setattr(settings, "db_path", tmp_path / "two.db")
    reset_engine()
    init_db()
    assert "two.db" in str(get_engine().url)
    reset_engine()


def test_init_db_creates_tables(db):
    with get_session() as session:
        assert session.get(DailyHealth, __import__("datetime").date(2026, 1, 1)) is None


def test_init_db_is_idempotent(db):
    init_db()
    init_db()
