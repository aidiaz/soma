"""Map Wahoo workouts into ``activities`` and write them.

Wahoo is the source of truth for virtual and trainer rides. Garmin records none
of them — a fifteen-day sync returned zero activities while Wahoo held the same
period's riding — so for this athlete this module is not a fidelity upgrade, it
is where training load comes from at all. See issue #3.

**On the units.** ``power_bike_tss_last`` is true TSS, unlike Garmin's
EPOC-derived training load. Both land in ``activities.tss``, so a week spanning
the two sources mixes quantities. That is tolerable here only because Garmin
contributes no rides; if it ever starts to, the ramp across that boundary is an
artefact of the change rather than of training.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from soma.clock import UTC, today
from soma.config import settings
from soma.db import get_session, init_db
from soma.ingest.wahoo.client import get_client
from soma.models import Activity

log = logging.getLogger("soma.ingest.wahoo")

SOURCE = "wahoo"

# Wahoo does not publish this enum in the documentation we can read, so it is
# built from ids actually observed and falls back rather than guessing. `raw`
# keeps the id, so a later correction is a remap and not a resync.
WORKOUT_TYPES = {
    12: "cycling",
    66: "yoga",
}


def _num(value: Any) -> float | None:
    """Coerce a Wahoo number to a float.

    Wahoo returns numerics as *strings* — ``"52.3"``, ``"365556.0"``. Fields
    assigned straight to the model survive because pydantic coerces them, but
    anything computed on the way in does not: a units conversion guarded by
    ``isinstance(x, float)`` silently produces None and the column reads as
    "no data" rather than failing. That is the same class of quiet wrongness as
    an empty-day placeholder row, so it is handled once, here.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sport(type_id: Any) -> str | None:
    if type_id is None:
        return None
    return WORKOUT_TYPES.get(int(type_id), f"wahoo_type_{type_id}")


def _local_wall_clock(iso_utc: str | None, zone: str | None) -> dt.datetime | None:
    """A Wahoo UTC instant as naive local wall-clock time.

    Wahoo reports an absolute ``started_at`` plus the ``time_zone`` it happened
    in, which is strictly better than Garmin's naive ``startTimeLocal``. Both
    end up naive here, because ``Activity.date`` is derived from this and a
    23:30 ride has to count towards that day's training rather than the next
    day's in some other zone.
    """
    if not iso_utc:
        return None
    try:
        # fromisoformat handles a trailing Z natively from 3.11; we require 3.12.
        moment = dt.datetime.fromisoformat(iso_utc)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if zone:
        try:
            moment = moment.astimezone(ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            # An unknown zone is worth saying out loud: every date derived from
            # it silently shifts, and date is the join key.
            log.warning("Unknown Wahoo time_zone %r; falling back to UTC.", zone)
    return moment.replace(tzinfo=None)


def map_workout(workout: dict[str, Any]) -> Activity | None:
    """A Wahoo workout to a row, or None if it is not a completed session.

    Wahoo returns scheduled workouts from the same endpoint as ridden ones — a
    plan laid out weeks ahead appears alongside history. Only entries carrying a
    ``workout_summary`` actually happened; the rest are Phase 5's problem, and
    writing them here would invent training that never occurred.
    """
    workout_id = workout.get("id")
    summary = workout.get("workout_summary")
    if workout_id is None or not summary:
        return None

    started = _local_wall_clock(
        summary.get("started_at") or workout.get("starts"), summary.get("time_zone")
    )
    if started is None:
        return None

    work_j = _num(summary.get("work_accum"))
    return Activity(
        source=SOURCE,
        external_id=str(workout_id),
        started_at=started,
        date=started.date(),
        sport=_sport(workout.get("workout_type_id")),
        duration_s=_num(summary.get("duration_active_accum")),
        # True TSS, which is what this column was always named for.
        tss=_num(summary.get("power_bike_tss_last")),
        avg_power=_num(summary.get("power_avg")),
        np=_num(summary.get("power_bike_np_last")),
        avg_hr=_num(summary.get("heart_rate_avg")),
        # Absent from every payload observed so far. Kept so it maps if Wahoo
        # starts sending it, rather than needing a code change to notice.
        max_hr=_num(summary.get("heart_rate_max")),
        # Wahoo reports joules; the column is kilojoules.
        work_kj=(work_j / 1000.0) if work_j is not None else None,
        raw=workout,
    )


def sync(days_back: int | None = None, dry_run: bool = False) -> dict[str, int]:
    """Pull workouts and upsert the completed ones inside the window."""
    init_db()
    window = days_back if days_back is not None else settings.wahoo_sync_days_back
    cutoff = today() - dt.timedelta(days=window)

    with get_client() as client:
        workouts = client.workouts()

    scheduled = 0
    written = 0
    outside = 0
    for workout in workouts:
        row = map_workout(workout)
        if row is None:
            scheduled += 1
            continue
        if row.date < cutoff:
            outside += 1
            continue
        if not dry_run:
            with get_session() as session:
                session.merge(row)
                session.commit()
        written += 1

    log.info(
        "Wahoo sync%s: %d completed workouts upserted, %d scheduled skipped, "
        "%d outside the %d-day window.",
        " (dry run)" if dry_run else "",
        written,
        scheduled,
        outside,
        window,
    )
    return {"written": written, "scheduled": scheduled, "outside": outside}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Sync Wahoo workouts into soma.")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Days back to sync (default from SOMA_WAHOO_SYNC_DAYS_BACK).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and map, but write nothing. Use this first against a new account.",
    )
    args = parser.parse_args()
    sync(days_back=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
