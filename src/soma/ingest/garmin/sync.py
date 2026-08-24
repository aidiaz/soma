"""Garmin ingestion: pull recent data and upsert into SQLite.

Garmin is the fragile source — an unofficial API behind TLS fingerprinting, with
a per-account rate limit and no contract. It is isolated here so that when it
breaks, nothing else stops.

Scope is deliberately narrow. Garmin is the **only** source for sleep, HRV,
Body Battery and resting heart rate, and that is what it is here for. It also
carries rides today; once Wahoo ingestion lands, activities move there and this
worker drops from five endpoints to three.

Design notes:
- Token-only auth (see :mod:`soma.ingest.garmin.client`), never a credential
  login — that is what risks the account lockout.
- Every row keeps the untouched payload in ``raw``, so unmapped fields are never
  lost and mappings can be refined later without a resync.
- Each endpoint is wrapped so one bad response logs a warning and the run
  continues instead of aborting.
"""

from __future__ import annotations

import argparse
import logging
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

from garminconnect import Garmin, GarminConnectTooManyRequestsError

from soma.clock import UTC, today
from soma.config import settings
from soma.db import get_session, init_db
from soma.ingest.garmin.client import get_client
from soma.models import WATCH_DERIVED_FIELDS, Activity, DailyHealth

log = logging.getLogger("soma.ingest.garmin")

SOURCE = "garmin"

# Fields that carry no measurement: keys and the raw payload. A row whose every
# *other* field is None told us nothing.
_NON_DATA_FIELDS = frozenset({"id", "date", "source", "external_id", "raw"})


# --------------------------------------------------------------------------- #
# write guard
# --------------------------------------------------------------------------- #
def _is_placeholder(row: Any) -> bool:
    """Whether ``row`` holds only its keys — no measurement at all."""
    return all(
        value is None for name, value in row.model_dump().items() if name not in _NON_DATA_FIELDS
    )


def _write(session: Any, row: Any) -> bool:
    """Merge ``row``, unless it is missing or a key-only placeholder.

    Garmin answers a day the watch never recorded with an empty payload rather
    than a 404. Mapping that produced a row containing nothing but a date, and
    such rows are worse than absent: they drag every weekly average toward
    nothing and make the coverage report claim days that hold no data. Refuse
    them here, at the only place that writes.
    """
    if row is None:
        return False
    if _is_placeholder(row):
        log.debug("skipping empty payload for %s", type(row).__name__)
        return False
    session.merge(row)
    return True


# --------------------------------------------------------------------------- #
# small extraction helpers
# --------------------------------------------------------------------------- #
def _pick(data: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return default


def _parse_dt(value: Any) -> datetime | None:
    """A Garmin timestamp to a naive wall-clock datetime.

    Naive on purpose, and it is not an oversight the linter should fix.
    ``map_activity`` prefers ``startTimeLocal`` and derives the training date
    from it, so that a 23:30 ride counts towards that day rather than the next
    one. Attaching a timezone here would move exactly the boundary that field
    is chosen to pin. That question is separate from what :mod:`soma.clock`
    answers, which is what day it is *now*.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            # Epoch millis are an absolute instant, so rendering them as a wall
            # clock needs a zone. UTC, matching soma.clock. Without it the
            # zone was whatever the process happened to run in, and the same
            # payload produced a different date on the Pi than on a laptop.
            return datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        cleaned = value.replace("Z", "").replace("T", " ").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(cleaned, fmt)  # noqa: DTZ007 - see docstring
            except ValueError:
                continue
    return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        dt = _parse_dt(value)
        return dt.date() if dt else None
    return None


def _daterange(start: date, end: date):
    for i in range((end - start).days + 1):
        yield start + timedelta(days=i)


def _jitter() -> None:
    time.sleep(random.uniform(settings.garmin_request_delay_min, settings.garmin_request_delay_max))


# --------------------------------------------------------------------------- #
# mappers
# --------------------------------------------------------------------------- #
def map_activity(a: dict[str, Any]) -> Activity | None:
    """Garmin activity payload to a row.

    ``activityTrainingLoad`` is mapped to ``tss``. They are not the same
    quantity — Garmin's is EPOC-derived — but they occupy the same role, and a
    ramp computed from either is a ratio. See :mod:`soma.metrics`.
    """
    activity_id = a.get("activityId")
    if activity_id is None:
        return None
    started = _parse_dt(_pick(a, "startTimeLocal", "startTimeGMT"))
    if started is None:
        return None
    atype = a.get("activityType")
    type_key = atype.get("typeKey") if isinstance(atype, dict) else atype
    duration = _pick(a, "duration")
    avg_power = _pick(a, "avgPower", "averagePower")
    return Activity(
        source=SOURCE,
        external_id=str(activity_id),
        started_at=started,
        # Local time, so a 23:30 ride belongs to that day's training.
        date=started.date(),
        sport=type_key,
        duration_s=duration,
        tss=_pick(a, "activityTrainingLoad"),
        avg_power=avg_power,
        np=_pick(a, "normPower"),
        avg_hr=_pick(a, "averageHR"),
        max_hr=_pick(a, "maxHR"),
        # Garmin does not report kJ. Average power over elapsed time is the
        # standard identity (1 W for 1 s = 1 J), close enough to be useful and
        # recomputed from raw if it ever needs to be exact.
        work_kj=round(avg_power * duration / 1000, 1)
        if avg_power is not None and duration
        else None,
        raw=a,
    )


def map_daily_health(
    d: date,
    stats: Any = None,
    sleep: Any = None,
    hrv: Any = None,
    training_status: Any = None,
    max_metrics: Any = None,
) -> DailyHealth:
    """Collapse a day's five Garmin payloads into one row.

    ``training_status`` and ``max_metrics`` get no typed columns — the schema
    has no home for VO2 max or Garmin's training-status phrase. They are still
    fetched and stored in ``raw`` because dropping them is irreversible: Garmin
    ages data out, so a later decision to want them back could not be honoured
    for history. Two extra calls a night is a cheap option to hold.
    """
    dto = sleep.get("dailySleepDTO") or {} if isinstance(sleep, dict) else {}
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    summary = hrv.get("hrvSummary") or {} if isinstance(hrv, dict) else {}
    return DailyHealth(
        date=d,
        sleep_score=_pick(overall, "value") or _pick(dto, "sleepScoreValue"),
        sleep_duration_s=_pick(dto, "sleepTimeSeconds"),
        hrv_status=_pick(summary, "status"),
        hrv_ms=_pick(summary, "lastNightAvg"),
        resting_hr=_pick(stats, "restingHeartRate"),
        body_battery_high=_pick(stats, "bodyBatteryHighestValue"),
        body_battery_low=_pick(stats, "bodyBatteryLowestValue"),
        steps=_pick(stats, "totalSteps"),
        raw={
            "stats": stats,
            "sleep": sleep,
            "hrv": hrv,
            "training_status": training_status,
            "max_metrics": max_metrics,
        },
    )


# --------------------------------------------------------------------------- #
# sync steps
# --------------------------------------------------------------------------- #
def _sync_activities(client: Garmin, start: date, end: date) -> int:
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    if not isinstance(activities, list):
        return 0
    count = 0
    with get_session() as session:
        for a in activities:
            row = map_activity(a) if isinstance(a, dict) else None
            if row is None:
                continue
            # merge() keys on the primary key, which is a surrogate here, so an
            # upsert has to find the existing row by (source, external_id) first
            # or every run would insert duplicates.
            existing = session.exec(_by_external_id(row.source, row.external_id)).first()
            if existing is not None:
                row.id = existing.id
            if _write(session, row):
                count += 1
        session.commit()
    return count


def _by_external_id(source: str, external_id: str):
    from sqlmodel import col, select

    return select(Activity).where(
        col(Activity.source) == source, col(Activity.external_id) == external_id
    )


def _fetch(label: str, d: date, fn) -> Any:
    """Call a per-day Garmin getter, converting failures into a logged warning."""
    try:
        return fn()
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("  %s failed for %s: %s", label, d, exc)
        return None


def sync_day(client: Garmin, d: date) -> int:
    """Fetch one day across every per-day endpoint. Returns rows written (0 or 1)."""
    iso = d.isoformat()
    row = map_daily_health(
        d,
        stats=_fetch("stats", d, lambda: client.get_stats(iso)),
        sleep=_fetch("sleep", d, lambda: client.get_sleep_data(iso)),
        hrv=_fetch("hrv", d, lambda: client.get_hrv_data(iso)),
        training_status=_fetch("training_status", d, lambda: client.get_training_status(iso)),
        max_metrics=_fetch("max_metrics", d, lambda: client.get_max_metrics(iso)),
    )
    with get_session() as session:
        written = _write(session, row)
        session.commit()
    return int(written)


def _day_is_synced(d: date) -> bool:
    """Whether the *watch* has reported for this day.

    Row existence is not the test. A phone-only day stores a step count, which
    is real data but none of the recovery signals, and treating it as complete
    is what let ``--skip-existing`` skip such days permanently.
    """
    with get_session() as session:
        row = session.get(DailyHealth, d)
        if row is None:
            return False
        return any(getattr(row, name, None) is not None for name in WATCH_DERIVED_FIELDS)


def _should_fetch(
    d: date,
    end: date,
    *,
    skip_existing: bool,
    refresh_days: int,
    recheck_days: int,
) -> bool:
    """Whether ``d`` is worth requesting on this run.

    Three windows, outermost first:

    - Inside ``refresh_days``: always. Garmin finishes processing a night after
      it ends, so the last day or two change under us.
    - Inside ``recheck_days`` and not yet synced by the watch: yes. This is the
      case the old row-existence test got wrong — a watch that uploads days
      later brings sleep and HRV with it, and nothing else will go back for it.
    - Older than ``recheck_days``: no. Some days are permanently phone-only
      because the watch was not worn, and retrying them nightly forever turns a
      year-long window into a year of dead requests every night.
    """
    if not skip_existing:
        return True
    if d >= end - timedelta(days=refresh_days):
        return True
    if _day_is_synced(d):
        return False
    return d >= end - timedelta(days=recheck_days)


def sync(
    days_back: int | None = None,
    skip_existing: bool = False,
    refresh_days: int = 2,
    recheck_days: int = 30,
) -> None:
    init_db()
    client = get_client()
    name = client.get_full_name() or getattr(client, "display_name", "account")
    end = today()
    start = end - timedelta(
        days=days_back if days_back is not None else settings.garmin_sync_days_back
    )
    total = (end - start).days + 1
    log.info("Authenticated as %s. Syncing %s .. %s (%s days).", name, start, end, total)
    if total > 90:
        log.info(
            "Large backfill: up to ~%s per-day requests — this can take many minutes. "
            "A 429 pauses it but saved days are kept%s.",
            total * 5,
            "; re-run with --skip-existing to resume"
            if not skip_existing
            else " and skipped next run",
        )

    try:
        n_act = _sync_activities(client, start, end)
        log.info("Activities upserted: %s", n_act)
        _jitter()

        synced = skipped = 0
        for i, d in enumerate(_daterange(start, end), start=1):
            if not _should_fetch(
                d,
                end,
                skip_existing=skip_existing,
                refresh_days=refresh_days,
                recheck_days=recheck_days,
            ):
                skipped += 1
                continue
            sync_day(client, d)
            synced += 1
            _jitter()
            if i % 30 == 0:
                log.info(
                    "  progress: %s/%s days (synced=%s, skipped=%s)", i, total, synced, skipped
                )
        log.info("Per-day health done: synced=%s, skipped=%s.", synced, skipped)
    except GarminConnectTooManyRequestsError:
        log.error(
            "Hit Garmin 429 rate limit mid-sync. Stopping; partial data is saved. "
            "Retry later (re-run with --skip-existing to resume) — do not loop."
        )
        raise SystemExit(1)

    log.info("Sync complete.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Sync Garmin data into soma.")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Days back to sync (default from SOMA_GARMIN_SYNC_DAYS_BACK).",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip days already stored (older than --refresh-days). Makes a year "
        "backfill resumable and nightly runs cheap.",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=2,
        help="Always re-fetch the most recent N days even with --skip-existing (default 2).",
    )
    parser.add_argument(
        "--recheck-days",
        type=int,
        default=30,
        help="Keep re-checking days the watch has not reported for, this far back "
        "(default 30). A watch that syncs late brings sleep and HRV with it; past "
        "this window a day is accepted as permanently phone-only.",
    )
    args = parser.parse_args()
    sync(
        days_back=args.days,
        skip_existing=args.skip_existing,
        refresh_days=args.refresh_days,
        recheck_days=args.recheck_days,
    )


if __name__ == "__main__":
    main()
