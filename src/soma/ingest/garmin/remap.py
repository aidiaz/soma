"""Re-derive typed columns from stored ``raw`` payloads — no Garmin calls.

Run this after improving a mapping in :mod:`soma.ingest.garmin.sync` to
backfill already-synced rows in place. Safe to run repeatedly; it only rewrites
typed columns from JSON already in the database.

This is the recovery path when Garmin renames a field, and the reason every
ingested row keeps its payload whole. Garmin ages data out, so re-fetching may
not be possible even when re-extracting still is.

Writes go through :func:`soma.ingest.garmin.sync._write`, so a mapping that
regressed to extracting nothing leaves the existing row alone rather than
flattening it into a placeholder.
"""

from __future__ import annotations

import logging

from sqlmodel import col, select

from soma.db import get_session, init_db
from soma.ingest.garmin.sync import SOURCE, _write, map_activity, map_daily_health
from soma.models import Activity, DailyHealth

log = logging.getLogger("soma.ingest.garmin.remap")


def remap() -> None:
    init_db()
    counts: dict[str, int] = {"activities": 0, "daily_health": 0}
    with get_session() as session:
        for row in session.exec(select(Activity).where(col(Activity.source) == SOURCE)).all():
            new = map_activity(row.raw)
            if new is None:
                continue
            # Keep the surrogate key: the row already exists, this is a rewrite.
            new.id = row.id
            counts["activities"] += _write(session, new)

        for row in session.exec(select(DailyHealth)).all():
            raw = row.raw or {}
            counts["daily_health"] += _write(
                session,
                map_daily_health(
                    row.date,
                    stats=raw.get("stats"),
                    sleep=raw.get("sleep"),
                    hrv=raw.get("hrv"),
                    training_status=raw.get("training_status"),
                    max_metrics=raw.get("max_metrics"),
                ),
            )

        session.commit()

    log.info("Rewrote rows from raw: %s", counts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    remap()


if __name__ == "__main__":
    main()
