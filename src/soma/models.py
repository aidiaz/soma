"""SQLModel tables.

One database, four writers, one read surface. The shape follows from a single
question — "here is my week, what should change?" — which needs rides, sleep,
food and body measurements correlated by date. So ``date`` is the join key
everywhere and is indexed on the one table where it is not the primary key.

Tables that ingest a vendor payload keep it whole in a ``raw`` column. Vendors
rename fields without notice; ``raw`` is what makes the old value recoverable
afterwards, which matters because Garmin ages data out and a resync may not be
able to fetch it back.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class Activity(SQLModel, table=True):
    """A completed session, from any source.

    The primary key is a surrogate rather than the vendor's id: the same ride
    can arrive from both Wahoo and Garmin, and those are two rows describing one
    effort, not a collision to be resolved at write time. Uniqueness is on
    ``(source, external_id)`` — scoping it that way means two vendors are free
    to number their records however they like.
    """

    __tablename__ = "activities"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_activity_source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # "wahoo" | "garmin"
    external_id: str
    started_at: dt.datetime | None = None
    # The join key to every other table. Derived from started_at in local time,
    # because a 23:30 ride belongs to that day's training, not the next one's.
    date: dt.date = Field(index=True)
    sport: str | None = None
    duration_s: float | None = None
    tss: float | None = None
    avg_power: float | None = None
    np: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    work_kj: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


# Columns only a watch can produce. ``steps`` is deliberately excluded: Garmin
# Connect records those from the phone, so a day holding steps and nothing else
# is a day the watch never uploaded. Both the sync worker and the coverage
# report need this distinction, and neither may import the other.
WATCH_DERIVED_FIELDS = (
    "sleep_score",
    "sleep_duration_s",
    "hrv_status",
    "hrv_ms",
    "resting_hr",
    "body_battery_high",
    "body_battery_low",
)


class DailyHealth(SQLModel, table=True):
    """One row per day. Garmin is the only source for every field here.

    Collapsed from three tables — sleep, HRV and daily stats each held one row
    per day keyed on the same date, so they were a single row wearing three
    hats. Anything Garmin returns beyond these columns stays in ``raw``.
    """

    __tablename__ = "daily_health"

    date: dt.date = Field(primary_key=True)
    sleep_score: int | None = None
    sleep_duration_s: int | None = None
    hrv_status: str | None = None
    hrv_ms: float | None = None
    resting_hr: int | None = None
    body_battery_high: int | None = None
    body_battery_low: int | None = None
    steps: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class IntakeEntry(SQLModel, table=True):
    """One thing consumed, at a time. Append-only.

    Manual, and permanently so — no sensor knows what you ate. Reporting food in
    conversation is what creates the record.

    **Entries, not a daily row.** The daily-total shape it replaces could only be
    written once: a second call overwrote the first and nulled any field it did
    not repeat, so logging lunch destroyed breakfast. Nobody knows their day's
    total at the moment they eat, which made the only tool for the job unusable
    for the way people actually eat.

    There is no ``kind`` column. Food, water and supplements differ by which
    fields are populated, not by a type tag — a recovery shake carries macros
    *and* ``ml``, and a classification would have to pick one. Supplements carry
    ``qty``/``unit`` (1.5 scoops, 2 pills) and no macros.

    ``at`` is an absolute UTC instant; ``date`` is the local day it belongs to,
    resolved by :mod:`soma.clock`. Both are stored because the first is the
    truth and the second is the join key.

    Corrections are a further entry, since the write tools have no delete.
    """

    __tablename__ = "intake_entries"

    id: int | None = Field(default=None, primary_key=True)
    at: dt.datetime
    date: dt.date = Field(index=True)
    item: str | None = None
    qty: float | None = None
    unit: str | None = None
    kcal: int | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    ml: int | None = None
    note: str | None = None


class Body(SQLModel, table=True):
    """Weekly measurements. Waist is the primary signal, not weight."""

    __tablename__ = "body"

    date: dt.date = Field(primary_key=True)
    weight_kg: float | None = None
    waist_cm: float | None = None


class FitnessTest(SQLModel, table=True):
    """4DP and FTP results.

    Class deliberately not named ``Test``: pytest collects any class matching
    ``Test*`` that a test module imports, and would report it as a broken test.
    """

    __tablename__ = "tests"

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    test_type: str | None = None  # "ftp" | "4dp" | "ramp"
    ftp: int | None = None
    map_w: int | None = None
    ac_w: int | None = None
    nm_w: int | None = None
    lthr: int | None = None
    weight_kg: float | None = None
    rider_type: str | None = None


class PlannedWorkout(SQLModel, table=True):
    """A session that was scheduled, as opposed to one that happened.

    Populated only if SYSTM plan data turns out to be reachable. Unused until
    then, and the tables around it do not depend on it.
    """

    __tablename__ = "planned_workouts"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_planned_source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    external_id: str
    date: dt.date = Field(index=True)
    name: str | None = None
    workout_type: str | None = None
    duration_s: float | None = None
    target_summary: str | None = None


class CalendarSync(SQLModel, table=True):
    """What makes calendar sync idempotent.

    Without this map, every run creates duplicates. With it, each run is a
    reconcile: absent here means create, hash changed means update in place,
    gone from the source means delete.
    """

    __tablename__ = "calendar_sync"

    workout_id: str = Field(primary_key=True)
    google_event_id: str
    last_synced_at: dt.datetime | None = None
    # Hash of the fields that appear in the calendar entry. Comparing it is how
    # a run tells "unchanged" from "edited" without diffing every field.
    content_hash: str | None = None


# The sources expected to report a run. A source absent from ``sync_runs``
# entirely must read as "never ran" rather than vanish from the report, so the
# reader needs the list rather than deriving it from the rows it found. Kept
# here for the same reason as ``WATCH_DERIVED_FIELDS``: the writer lives in
# ``ingest`` and the reader in ``serve``, and neither may import the other.
SYNC_SOURCES = ("garmin", "wahoo")

# "running" is written before the work starts. A row still saying so hours
# later is the only trace a worker that was killed mid-run leaves behind.
SYNC_RUNNING = "running"
SYNC_OK = "ok"
SYNC_FAILED = "failed"


class SyncRun(SQLModel, table=True):
    """One attempt by a sync worker, whether or not it wrote anything.

    Without this table the only answer to "did ingestion run" is the newest
    data row, and that cannot tell a worker which ran and found nothing from
    one that has been dead for a week. Both look like a rest day — the same
    class of quiet wrongness as an empty-day placeholder row, and the reason
    ``coverage`` exists at all.

    The row is written twice: once at the start, so a process killed mid-run
    still leaves a record, and again at the end with the outcome. Both
    timestamps are absolute instants, stored naive in UTC like every other
    timestamp here.

    ``counts`` is per-source and deliberately untyped. Garmin counts days and
    activities, Wahoo counts workouts and the scheduled ones it skipped, and
    forcing those into shared columns would mean columns that are null for
    half the rows and a migration every time a worker learns to count
    something else.

    Nothing prunes this. A run an hour from each of two workers is about 18k
    rows a year at a few hundred bytes each — small enough that a delete path,
    which is a way to lose evidence, costs more than the space.
    """

    __tablename__ = "sync_runs"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # "garmin" | "wahoo"
    started_at: dt.datetime = Field(index=True)
    finished_at: dt.datetime | None = None
    status: str = SYNC_RUNNING
    # What the run was asked to cover, so a short window explains a gap the
    # reader would otherwise read as missing data.
    window_days: int | None = None
    counts: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Type and message, truncated. The full traceback belongs in the log; this
    # is here so the failure is visible without shell access to the Pi.
    error: str | None = None


class SyncRequest(SQLModel, table=True):
    """Someone asked for a source to sync now, and its worker has not yet.

    This table is the whole interface between the MCP server and the sync
    workers. The server may not contact a vendor — only the workers hold vendor
    credentials, and only they can trip a rate limit — so the tool that asks for
    a resync cannot perform one. It writes a row here; the worker for that
    source reads it and runs. No socket, no port, no Docker API, and the ask
    survives a worker that happens to be restarting when it is made.

    ``served_at`` means *an attempt was made*, not that it succeeded. A failed
    run still marks its requests served, because the alternative is a request
    that stays pending and wakes the worker again immediately — a retry loop
    against a vendor that is already refusing, which on Garmin costs an account
    lockout rather than wasted time. The outcome is in ``sync_runs`` via
    ``run_id``.

    Nothing prunes this, for the same reason nothing prunes ``sync_runs``: the
    rows are the record of what was asked for and what came of it, and a delete
    path is a way to lose that.
    """

    __tablename__ = "sync_requests"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # "garmin" | "wahoo"
    requested_at: dt.datetime = Field(index=True)
    # Free text from the caller — why they asked. Kept because a queue with no
    # reasons cannot tell a habit from an incident afterwards.
    note: str | None = None
    served_at: dt.datetime | None = None
    # The sync_runs row that served it. Not a foreign key: sync_runs is written
    # by a different process and a request can outlive the bookkeeping row's
    # own write failing, which runs.py explicitly tolerates.
    run_id: int | None = None
