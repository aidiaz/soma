"""Asking for a sync on demand, and the worker noticing.

The MCP server may not contact a vendor, so `request_sync` cannot sync — it
appends a row that the vendor's worker reads. Everything worth testing lives in
the seams of that arrangement: a request must not be dropped, must not be served
twice, and must not turn a broken sync into a retry loop against a vendor whose
rate limit is per account.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from sqlmodel import col, select

from soma import sync_requests
from soma.clock import now
from soma.db import get_session
from soma.ingest import runs
from soma.ingest.runs import record_run
from soma.ingest.schedule import next_daily, wait_until
from soma.models import SYNC_FAILED, SYNC_OK, SyncRequest, SyncRun
from soma.serve import queries


def stored(source: str | None = None) -> list[SyncRequest]:
    with get_session() as session:
        stmt = select(SyncRequest).order_by(col(SyncRequest.id))
        if source is not None:
            stmt = stmt.where(col(SyncRequest.source) == source)
        return list(session.exec(stmt).all())


def record(source: str, status: str, *, ago_s: float) -> None:
    """A finished run, placed in the past. Stored naive UTC, as the writer does."""
    stamp = (now() - dt.timedelta(seconds=ago_s)).replace(tzinfo=None)
    with get_session() as session:
        session.add(
            SyncRun(source=source, started_at=stamp, finished_at=stamp, status=status, counts={})
        )
        session.commit()


# --------------------------------------------------------------------------- #
# the queue
# --------------------------------------------------------------------------- #
def test_a_request_is_stored_pending(db):
    row, created = sync_requests.enqueue("wahoo", note="just finished a ride")
    assert created
    assert row.served_at is None
    assert sync_requests.pending("wahoo") is not None
    assert stored("wahoo")[0].note == "just finished a ride"


def test_a_second_ask_coalesces_into_the_one_already_waiting(db):
    first, _ = sync_requests.enqueue("wahoo")
    second, created = sync_requests.enqueue("wahoo")
    assert not created
    assert second.id == first.id
    assert len(stored("wahoo")) == 1


def test_one_source_does_not_answer_for_another(db):
    sync_requests.enqueue("wahoo")
    assert sync_requests.pending("garmin") is None


def test_a_run_serves_the_requests_that_preceded_it(db):
    sync_requests.enqueue("garmin")
    with record_run("garmin"):
        pass

    (row,) = stored("garmin")
    assert row.served_at is not None
    assert row.run_id is not None
    assert sync_requests.pending("garmin") is None


def test_a_request_made_mid_run_is_left_for_the_next_one(db):
    # The run had already chosen its window before the ask existed, so serving
    # it would answer a question nobody got to ask. One extra run is the cheaper
    # error: the other kind is invisible to the person waiting.
    with record_run("garmin"):
        sync_requests.enqueue("garmin")

    assert sync_requests.pending("garmin") is not None


def test_a_failed_run_still_serves_its_requests(db):
    # Otherwise the request survives the failure, wakes the worker the moment it
    # starts waiting, and a broken sync becomes a hot retry loop against a
    # vendor that is already refusing.
    sync_requests.enqueue("garmin")
    with pytest.raises(RuntimeError), record_run("garmin"):
        raise RuntimeError("garmin said no")

    (row,) = stored("garmin")
    assert row.served_at is not None
    assert sync_requests.pending("garmin") is None


def test_a_rate_limit_exit_still_serves_its_requests(db):
    # The Garmin worker signals a rate limit with SystemExit, which is the
    # single case where a retry loop is most expensive.
    sync_requests.enqueue("garmin")
    with pytest.raises(SystemExit), record_run("garmin"):
        raise SystemExit(1)

    assert sync_requests.pending("garmin") is None


def test_serving_failure_does_not_break_the_sync(db, monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(runs, "mark_served", boom)
    with caplog.at_level(logging.ERROR), record_run("wahoo") as counts:
        counts["written"] = 1

    # The sync completed and was recorded; only the bookkeeping was lost.
    with get_session() as session:
        (run,) = session.exec(select(SyncRun)).all()
    assert run.status == SYNC_OK
    assert "Could not close the on-demand requests" in caplog.text


def test_served_rows_are_kept_not_deleted(db):
    sync_requests.enqueue("wahoo")
    with record_run("wahoo"):
        pass
    sync_requests.enqueue("wahoo")

    # The old request is still there, closed, beside the new pending one.
    rows = stored("wahoo")
    assert len(rows) == 2
    assert [r.served_at is None for r in rows] == [False, True]


# --------------------------------------------------------------------------- #
# the tool
# --------------------------------------------------------------------------- #
def test_the_default_asks_both_sources(db):
    out = queries.request_sync()
    assert set(out["sources"]) == {"garmin", "wahoo"}
    assert all(s["queued"] for s in out["sources"].values())
    assert len(stored()) == 2


def test_one_source_can_be_asked_alone(db):
    out = queries.request_sync(source="wahoo")
    assert set(out["sources"]) == {"wahoo"}
    assert [r.source for r in stored()] == ["wahoo"]


def test_an_unknown_source_is_refused(db):
    with pytest.raises(ValueError, match="unknown source"):
        queries.request_sync(source="strava")


def test_a_source_that_just_synced_is_not_asked_again(db):
    record("wahoo", SYNC_OK, ago_s=5)
    out = queries.request_sync(source="wahoo")["sources"]["wahoo"]

    assert out["queued"] is False
    assert "minimum gap" in out["reason"]
    assert stored() == []


def test_the_gap_is_short_enough_to_be_invisible_after_a_ride(db):
    record("wahoo", SYNC_OK, ago_s=queries.MIN_REQUEST_GAP_S + 1)
    assert queries.request_sync(source="wahoo")["sources"]["wahoo"]["queued"] is True


def test_asking_twice_reports_the_request_already_waiting(db):
    queries.request_sync(source="garmin")
    out = queries.request_sync(source="garmin")["sources"]["garmin"]

    # Still "queued": the ask is covered. It just did not create a second row.
    assert out["queued"] is True
    assert "already waiting" in out["reason"]
    assert len(stored("garmin")) == 1


def test_the_reply_carries_the_worker_health_it_is_asking(db):
    # A request handed to a worker that died on Tuesday is never served, and
    # nothing else in the reply would say so.
    record("garmin", SYNC_FAILED, ago_s=90_000)
    out = queries.request_sync(source="garmin")["sources"]["garmin"]
    assert out["worker"]["status"] == SYNC_FAILED

    fresh = queries.request_sync(source="wahoo")["sources"]["wahoo"]
    assert fresh["worker"]["status"] == "never"


def test_the_reply_says_how_long_the_worker_may_take_to_notice(db):
    assert queries.request_sync()["poll_seconds"] > 0


# --------------------------------------------------------------------------- #
# the wait
# --------------------------------------------------------------------------- #
@pytest.fixture
def utc(monkeypatch):
    """Pin the zone. ``settings`` reads the developer's real .env, so without
    this these assertions pass or fail depending on whose machine runs them —
    and the whole point of the function under test is that it is zone-aware."""
    from soma.config import settings

    monkeypatch.setattr(settings, "timezone", "UTC")


def test_the_next_daily_run_is_later_today_when_the_hour_has_not_passed(db, utc):
    after = dt.datetime(2026, 8, 30, 6, 0, tzinfo=dt.UTC)
    assert next_daily(after, dt.time(8, 0)).isoformat().startswith("2026-08-30T08:00")


def test_a_run_finishing_a_minute_early_still_waits_for_today(db, utc):
    after = dt.datetime(2026, 8, 30, 7, 59, tzinfo=dt.UTC)
    assert next_daily(after, dt.time(8, 0)).day == 30


def test_a_run_finishing_exactly_on_the_hour_goes_to_tomorrow(db, utc):
    # Otherwise the wait is zero seconds and the sync runs twice in a row.
    after = dt.datetime(2026, 8, 30, 8, 0, tzinfo=dt.UTC)
    assert next_daily(after, dt.time(8, 0)).day == 31


def test_the_hour_is_read_in_the_configured_zone_not_utc(db, monkeypatch):
    from soma.config import settings

    monkeypatch.setattr(settings, "timezone", "America/Santiago")
    # 09:00 UTC is 05:00 in Santiago, so today's 08:00 local is still ahead.
    after = dt.datetime(2026, 8, 30, 9, 0, tzinfo=dt.UTC)
    nxt = next_daily(after, dt.time(8, 0))
    assert nxt.hour == 8
    assert nxt.utcoffset() == dt.timedelta(hours=-4)
    assert (nxt - after).total_seconds() == 3 * 3600


def test_the_wait_returns_at_once_when_a_request_is_already_pending(db):
    sync_requests.enqueue("wahoo")
    deadline = now() + dt.timedelta(hours=1)
    assert wait_until("wahoo", deadline, poll_s=3600).startswith("requested at")


def test_the_wait_returns_when_the_deadline_has_passed(db):
    deadline = now() - dt.timedelta(seconds=1)
    assert wait_until("garmin", deadline, poll_s=3600) == "scheduled"


def test_the_wait_ignores_another_source_s_request(db):
    sync_requests.enqueue("wahoo")
    deadline = now() - dt.timedelta(seconds=1)
    assert wait_until("garmin", deadline, poll_s=3600) == "scheduled"
