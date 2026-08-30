"""Record that a sync worker ran, so silence can be told from failure.

Compose has no scheduler: both workers are `while true; sleep` loops, and a
loop that dies takes its next run with it. Until this module existed the only
evidence a run had happened was the newest data row, which answers a different
question — a worker that ran and found nothing looks exactly like a worker that
has been dead for a week, and both look like a rest day in the weekly report.

The writer lives here rather than in either vendor package because both need
it, and the reader (:mod:`soma.serve.queries`) needs neither.

**Recording must never break a sync.** A failure to write the bookkeeping row
is logged at ERROR — which `make smoke` scans for — and then dropped. Losing
the observation is bad; losing the night's ingestion because the observation
failed would be worse.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from soma.clock import now
from soma.db import get_session
from soma.models import SYNC_FAILED, SYNC_OK, SYNC_RUNNING, SyncRun
from soma.sync_requests import mark_served

log = logging.getLogger("soma.ingest.runs")

# Enough to identify the failure, short enough that a loop failing every hour
# for a month cannot fill an SD card with one vendor's stack trace.
ERROR_MAX = 500


def _start(source: str, window_days: int | None, started_at: dt.datetime) -> int | None:
    """Write the row before the work, so a killed process still leaves a trace."""
    row = SyncRun(
        source=source,
        started_at=started_at,
        status=SYNC_RUNNING,
        window_days=window_days,
    )
    with get_session() as session:
        session.add(row)
        session.commit()
        return row.id


def _finish(run_id: int | None, status: str, counts: dict[str, Any], error: str | None) -> None:
    if run_id is None:
        return
    with get_session() as session:
        row = session.get(SyncRun, run_id)
        if row is None:
            return
        row.finished_at = now().replace(tzinfo=None)
        row.status = status
        row.counts = dict(counts)
        row.error = error
        session.add(row)
        session.commit()


@contextmanager
def record_run(source: str, *, window_days: int | None = None) -> Iterator[dict[str, Any]]:
    """Record one run of ``source``. Yields a dict the caller fills with counts.

    Call it inside the sync function rather than around the CLI, so a run is
    recorded however the sync was invoked. The database must already be
    initialised — every caller does that first anyway.

    ``BaseException`` is caught on purpose, not by oversight: the Garmin worker
    signals a rate limit by raising ``SystemExit``, and that is the single
    failure most worth having a record of.
    """
    counts: dict[str, Any] = {}
    # Naive UTC, the convention every timestamp column here follows. Captured
    # before the bookkeeping write so it is still known if that write fails,
    # and so it is the same instant the request queue is closed against.
    started_at = now().replace(tzinfo=None)
    try:
        run_id = _start(source, window_days, started_at)
    except Exception:
        log.exception("Could not record the start of a %s sync run; continuing.", source)
        run_id = None

    try:
        yield counts
    except BaseException as exc:
        _safe_finish(run_id, SYNC_FAILED, counts, f"{type(exc).__name__}: {exc}"[:ERROR_MAX])
        _safe_serve(source, run_id, started_at)
        raise
    _safe_finish(run_id, SYNC_OK, counts, None)
    _safe_serve(source, run_id, started_at)


def _safe_finish(
    run_id: int | None, status: str, counts: dict[str, Any], error: str | None
) -> None:
    try:
        _finish(run_id, status, counts, error)
    except Exception:
        log.exception("Could not record the outcome of sync run %s.", run_id)


def _safe_serve(source: str, run_id: int | None, started_at: dt.datetime) -> None:
    """Close the on-demand requests this run answered.

    Runs on the failure path too. A request left pending after a failed attempt
    wakes the worker again the moment it starts waiting, which turns one broken
    sync into a retry loop against a vendor that is already refusing — and
    Garmin answers that with a per-account lockout that only time clears. The
    request records that an attempt was made; ``sync_runs`` records how it went.
    """
    try:
        served = mark_served(source, run_id, started_at)
        if served:
            log.info("Served %s on-demand %s sync request(s).", served, source)
    except Exception:
        log.exception("Could not close the on-demand requests for sync run %s.", run_id)
