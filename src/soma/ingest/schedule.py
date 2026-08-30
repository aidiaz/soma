"""Wait until the next scheduled sync, or until someone asks for one sooner.

Compose has no scheduler, so each worker is a `while true` loop and this is the
sleep in the middle of it. It replaces a plain `sleep`, which could not be
interrupted: a request for an on-demand sync written at 21:00 would have sat in
the queue until 08:00, which is the same as not having the feature.

Two jobs, and the second is why this is Python rather than four lines of shell:

- **When is the next run.** `sleep 86400` is not a daily schedule. It starts
  wherever the container happened to start, slips forward by each run's own
  duration, and moves an hour at every DST change. Recomputing the next 08:00
  from the clock each time fixes all three. That logic was shell until this
  module existed, and shell could be verified only by running it.
- **Has anyone asked.** Polling rather than a signal, because the requester is
  the server container and the only thing the two share is the database file.

The hour is read in the configured zone — the same one that decides the join
key — so "the 08:00 run" and "today" can never disagree about which day they
mean.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from soma.clock import local_zone, now
from soma.config import settings
from soma.db import init_db
from soma.models import SYNC_SOURCES
from soma.sync_requests import pending

log = logging.getLogger("soma.ingest.schedule")


def next_daily(after: dt.datetime, at: dt.time) -> dt.datetime:
    """The next occurrence of local wall time ``at``, strictly after ``after``.

    Strictly, because the two boundaries are the ones that bite: a run
    finishing at 07:59 must still wait for today's 08:00, and one finishing
    exactly at 08:00 must go to tomorrow rather than sleep zero seconds and
    immediately run again.

    Built in local wall time and converted back, so a DST change moves the run
    with the clock instead of an hour away from it.
    """
    zone = local_zone()
    local = after.astimezone(zone)
    candidate = dt.datetime.combine(local.date(), at, tzinfo=zone)
    if candidate <= local:
        candidate = dt.datetime.combine(local.date() + dt.timedelta(days=1), at, tzinfo=zone)
    return candidate


def wait_until(source: str, deadline: dt.datetime, poll_s: float) -> str:
    """Block until ``deadline``, or until ``source`` has a pending request.

    Returns a one-line reason, which the caller logs: a worker that woke early
    and a worker that woke on time look identical in the data afterwards, and
    only one of them means somebody was waiting.

    The queue is checked before the first sleep. A request made while the
    previous sync was still running is left pending on purpose
    (:func:`soma.sync_requests.mark_served`), and it should be served now rather
    than after a full cycle.
    """
    while True:
        request = pending(source)
        if request is not None:
            return f"requested at {request.requested_at.isoformat()}Z"
        remaining = (deadline - now()).total_seconds()
        if remaining <= 0:
            return "scheduled"
        time.sleep(min(poll_s, remaining))


def _deadline(at: str | None, every: float | None) -> dt.datetime:
    if at is not None:
        return next_daily(now(), dt.time.fromisoformat(at))
    return now() + dt.timedelta(seconds=every or 0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Sleep until a source's next sync is due, or until one is requested."
    )
    parser.add_argument("--source", required=True, choices=list(SYNC_SOURCES))
    schedule = parser.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--at", help="Local time of day to wake, HH:MM in SOMA_TIMEZONE.")
    schedule.add_argument("--every", type=float, help="Seconds until the next run.")
    parser.add_argument(
        "--poll",
        type=float,
        default=settings.sync_poll_s,
        help="How often to check for an on-demand request, in seconds "
        "(default from SOMA_SYNC_POLL_S). This is the worst-case delay between "
        "asking for a sync and the worker starting one.",
    )
    args = parser.parse_args()

    # The worker may be the first process to open a fresh volume, and a missing
    # table here would read as "nobody has ever asked" rather than as a fault.
    init_db()

    deadline = _deadline(args.at, args.every)
    log.info("next %s-sync at %s (or sooner if requested)", args.source, deadline.isoformat())
    log.info("waking %s-sync: %s", args.source, wait_until(args.source, deadline, args.poll))


if __name__ == "__main__":
    sys.exit(main())
