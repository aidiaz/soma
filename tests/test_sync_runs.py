"""Recording that a sync ran, and reporting it.

The failure this guards is not a wrong number, it is a *silence*. Both workers
are `while true; sleep` loops in compose. When one dies, no error reaches the
athlete: the database simply stops gaining days, and the weekly report shows a
gap that reads as a rest week. Every test here is about keeping those two
apart.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from sqlmodel import col, desc, select

from soma.clock import UTC, now
from soma.db import get_session
from soma.ingest import runs
from soma.ingest.runs import record_run
from soma.models import SYNC_FAILED, SYNC_OK, SYNC_RUNNING, SyncRun
from soma.serve import queries


def stored(source: str | None = None) -> list[SyncRun]:
    with get_session() as session:
        stmt = select(SyncRun).order_by(desc(col(SyncRun.started_at)), desc(col(SyncRun.id)))
        if source is not None:
            stmt = stmt.where(col(SyncRun.source) == source)
        return list(session.exec(stmt).all())


def record(source: str, status: str, *, ago_s: float, error: str | None = None) -> None:
    """A finished run, placed in the past. Stored naive UTC, as the writer does."""
    stamp = (now() - dt.timedelta(seconds=ago_s)).replace(tzinfo=None)
    with get_session() as session:
        session.add(
            SyncRun(
                source=source,
                started_at=stamp,
                finished_at=stamp,
                status=status,
                counts={"written": 1},
                error=error,
            )
        )
        session.commit()


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def test_a_successful_run_is_recorded_with_its_counts(db):
    with record_run("wahoo", window_days=30) as counts:
        counts["written"] = 3

    (row,) = stored()
    assert row.source == "wahoo"
    assert row.status == SYNC_OK
    assert row.counts == {"written": 3}
    assert row.window_days == 30
    assert row.finished_at is not None
    assert row.error is None


def test_the_row_exists_before_the_work_does(db):
    """A process killed mid-run leaves this row and nothing else.

    Writing only on completion would lose exactly the runs worth knowing about.
    """
    with record_run("garmin"):
        (during,) = stored()
        assert during.status == SYNC_RUNNING
        assert during.finished_at is None


def test_a_failure_is_recorded_and_re_raised(db):
    with pytest.raises(RuntimeError, match="token expired"), record_run("garmin"):
        raise RuntimeError("token expired")

    (row,) = stored()
    assert row.status == SYNC_FAILED
    assert row.error == "RuntimeError: token expired"
    assert row.finished_at is not None


def test_a_rate_limit_exit_is_recorded(db):
    """``SystemExit`` is not an ``Exception``, and it is how a 429 stops the run.

    Catching only ``Exception`` here would lose the single failure most worth
    a record — the one where retrying costs a per-account cooldown.
    """
    with pytest.raises(SystemExit), record_run("garmin"):
        raise SystemExit("Garmin 429 rate limit — stopped mid-sync, partial data saved")

    (row,) = stored()
    assert row.status == SYNC_FAILED
    assert row.error is not None
    assert "429" in row.error


def test_a_long_error_is_truncated(db):
    with pytest.raises(RuntimeError), record_run("wahoo"):
        raise RuntimeError("x" * 5_000)

    (row,) = stored()
    assert row.error is not None
    assert len(row.error) == runs.ERROR_MAX


def test_recording_failure_does_not_break_the_sync(db, monkeypatch, caplog):
    """Bookkeeping must never cost an ingestion.

    Losing the observation is bad. Losing the night's data because writing the
    observation failed would be worse — so it is logged at ERROR, which
    ``make smoke`` scans for, and then dropped.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runs, "_finish", boom)
    with caplog.at_level(logging.ERROR), record_run("wahoo") as counts:
        counts["written"] = 1

    assert "Could not record the outcome" in caplog.text


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def test_a_source_that_never_ran_says_so(db):
    """Explicit, not absent. A worker never deployed must not read as healthy."""
    status = queries.sync_status_by_source()
    assert set(status) == {"garmin", "wahoo"}
    assert status["garmin"]["status"] == "never"
    assert status["garmin"]["last_run_at"] is None
    assert status["garmin"]["last_success_at"] is None
    assert status["garmin"]["seconds_since_success"] is None


def test_the_last_success_is_reported_with_its_age(db):
    record("wahoo", SYNC_OK, ago_s=3_600)
    wahoo = queries.sync_status_by_source()["wahoo"]
    assert wahoo["status"] == SYNC_OK
    assert wahoo["seconds_since_success"] == pytest.approx(3_600, abs=30)


def test_a_failure_after_a_success_reports_the_failure(db):
    record("garmin", SYNC_OK, ago_s=90_000)
    record("garmin", SYNC_FAILED, ago_s=600, error="GarminConnectAuthenticationError: 401")

    garmin = queries.sync_status_by_source()["garmin"]
    assert garmin["status"] == SYNC_FAILED
    assert garmin["error"] is not None
    # The last success stays visible: "broken since yesterday" is the useful
    # statement, and it needs both halves.
    assert garmin["last_success_at"] is not None
    assert garmin["seconds_since_success"] == pytest.approx(90_000, abs=30)


def test_an_old_failure_is_not_reported_once_a_later_run_succeeded(db):
    record("wahoo", SYNC_FAILED, ago_s=7_200, error="RuntimeError: refresh rejected")
    record("wahoo", SYNC_OK, ago_s=60)

    wahoo = queries.sync_status_by_source()["wahoo"]
    assert wahoo["status"] == SYNC_OK
    assert wahoo["error"] is None


def test_a_run_that_never_finished_still_reads_as_running(db):
    with get_session() as session:
        session.add(
            SyncRun(
                source="garmin",
                started_at=(now() - dt.timedelta(hours=9)).replace(tzinfo=None),
                status=SYNC_RUNNING,
            )
        )
        session.commit()

    garmin = queries.sync_status_by_source()["garmin"]
    assert garmin["status"] == SYNC_RUNNING
    assert garmin["last_success_at"] is None


def test_recent_runs_are_listed_and_capped(db):
    for i in range(queries.MAX_LISTED_RUNS + 3):
        record("wahoo", SYNC_OK, ago_s=3_600 * (i + 1))

    report = queries.get_sync_status()
    recent = report["sources"]["wahoo"]["recent_runs"]
    assert len(recent) == queries.MAX_LISTED_RUNS
    # Newest first, so a run of failures is visible at the top.
    stamps = [r["started_at"] for r in recent]
    assert stamps == sorted(stamps, reverse=True)
    assert report["sources"]["garmin"]["recent_runs"] == []


def test_the_week_carries_the_sync_report_beside_its_gaps(db):
    """Coverage is where a gap is read, so it is where this belongs.

    A missing day and a dead worker produce the same empty week. The report has
    to answer both in one place or the ambiguity resolves the flattering way.
    """
    record("wahoo", SYNC_FAILED, ago_s=300, error="RuntimeError: refresh rejected")
    coverage = queries.get_training_week()["coverage"]
    assert coverage["sync"]["wahoo"]["status"] == SYNC_FAILED
    assert coverage["sync"]["garmin"]["status"] == "never"


def test_stored_timestamps_are_utc_not_local(db):
    """The age is a subtraction, so a naive local timestamp would skew it silently."""
    with record_run("wahoo"):
        pass

    (row,) = stored()
    assert row.finished_at is not None
    drift = abs((now() - row.finished_at.replace(tzinfo=UTC)).total_seconds())
    assert drift < 60, "stored timestamp is not UTC"


# --------------------------------------------------------------------------- #
# the workers
# --------------------------------------------------------------------------- #
class FakeWahooClient:
    def __init__(self, workouts: list[dict]) -> None:
        self._workouts = workouts

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def workouts(self) -> list[dict]:
        return self._workouts


def wahoo_workout() -> dict:
    stamp = now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": 1,
        "starts": stamp,
        "workout_type_id": 12,
        "workout_summary": {
            "id": 2,
            "started_at": stamp,
            "time_zone": "UTC",
            "power_bike_tss_last": "40.0",
            "duration_active_accum": "3600.0",
        },
    }


def test_a_wahoo_sync_records_what_it_wrote(db, monkeypatch):
    from soma.ingest.wahoo import sync as wahoo_sync

    monkeypatch.setattr(wahoo_sync, "get_client", lambda: FakeWahooClient([wahoo_workout()]))
    wahoo_sync.sync()

    (row,) = stored("wahoo")
    assert row.status == SYNC_OK
    assert row.counts["written"] == 1


def test_a_wahoo_dry_run_is_not_recorded(db, monkeypatch):
    """A dry run writes nothing, so it must not advance "last successful sync"."""
    from soma.ingest.wahoo import sync as wahoo_sync

    monkeypatch.setattr(wahoo_sync, "get_client", lambda: FakeWahooClient([wahoo_workout()]))
    wahoo_sync.sync(dry_run=True)

    assert stored("wahoo") == []


def test_a_wahoo_sync_that_cannot_authenticate_is_recorded(db, monkeypatch):
    from soma.ingest.wahoo import sync as wahoo_sync

    def no_token():
        raise RuntimeError("No Wahoo tokens stored")

    monkeypatch.setattr(wahoo_sync, "get_client", no_token)
    with pytest.raises(RuntimeError):
        wahoo_sync.sync()

    (row,) = stored("wahoo")
    assert row.status == SYNC_FAILED
    assert row.error is not None
    assert "No Wahoo tokens" in row.error
