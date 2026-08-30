"""Read and write functions behind the MCP tools.

The join happens here, in SQL and plain Python, not in a context window. One
call to :func:`get_training_week` returns rides, sleep, HRV, food and body
measurements already correlated by date, because that correlation is the whole
reason the database exists.

**Measurements here, judgement elsewhere.** These functions compute what the
coaching rules need — average intake, TSS ramp, the change in resting heart rate
against the prior week — and stop there. The thresholds that turn "resting HR up
6 bpm" into "cut weekly TSS by 30%" live in the coaching skill, version
controlled, so they can be revised without a schema change and reviewed without
reading Python.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlmodel import col, desc, select

from soma import sync_requests
from soma.clock import UTC, now, today
from soma.config import settings
from soma.db import get_session
from soma.metrics import duplicate_efforts, training_load_series, tss_ramp
from soma.models import (
    SYNC_OK,
    SYNC_SOURCES,
    WATCH_DERIVED_FIELDS,
    Activity,
    Body,
    DailyHealth,
    FitnessTest,
    IntakeEntry,
    SyncRun,
)

# How far back to warm the CTL/ATL EWMAs before reading them. CTL's time
# constant is 42 days, so a shorter window reports fitness that is an artefact
# of where the window started.
WARMUP_DAYS = 120

# Coverage gaps are listed by date rather than counted, because "which days"
# is what tells a failed sync apart from a rest day. Long backfills can be
# missing hundreds, so the list is capped and the true count sent alongside.
MAX_LISTED_GAPS = 14

# How many past runs the sync report carries per source. Enough to see "failing
# every hour since Tuesday" rather than a single red result with no shape.
MAX_LISTED_RUNS = 5

# Reported, never stored: no row means no run, and the source has to say that
# out loud rather than be left out of the report.
NEVER = "never"


def _dump(obj: Any, include_raw: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = obj.model_dump(mode="json")
    if not include_raw:
        data.pop("raw", None)
    return data


def _mean(values: list[Any], ndigits: int = 1) -> float | None:
    """Average of the non-null values, or None when there are none.

    Nulls are dropped rather than counted as zero. A day the watch recorded
    steps but no HRV must not pull the HRV average down, and a day with no food
    logged is unknown intake, not a fast.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present) / len(present), ndigits)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_week_start(week_start: str | None) -> date:
    """Resolve the requested week to the Monday that contains it.

    Any date inside a week resolves to that week, so "the week of the 14th"
    works without the caller needing a calendar.
    """
    if week_start is None:
        return _monday(today())
    return _monday(date.fromisoformat(week_start))


def _gaps(present: set[date], start: date, end: date) -> dict[str, Any]:
    missing = [
        d.isoformat()
        for d in (start + timedelta(days=i) for i in range((end - start).days + 1))
        if d not in present
    ]
    return {
        "days_present": (end - start).days + 1 - len(missing),
        "days_missing": len(missing),
        "missing": missing[:MAX_LISTED_GAPS],
    }


def _health_coverage(rows: list[DailyHealth], start: date, end: date) -> dict[str, Any]:
    """Coverage for health, counting what the watch reported rather than rows.

    A row can exist and still hold nothing the recovery rules can use: Garmin
    logs steps from the phone on days the watch never synced. Counting those as
    present made the report claim complete data for days that answer nothing,
    which is the failure coverage exists to prevent — a gap must not read as a
    rest day.

    ``by_signal`` is here because "present" is not one thing. A day can carry a
    resting heart rate and no sleep, and a reader deciding whether to trust a
    sleep average needs to know that specifically.
    """
    watched = {
        r.date for r in rows if any(getattr(r, f, None) is not None for f in WATCH_DERIVED_FIELDS)
    }
    coverage = _gaps(watched, start, end)
    coverage["by_signal"] = {
        field: sum(1 for r in rows if getattr(r, field, None) is not None)
        for field in WATCH_DERIVED_FIELDS
    }
    return coverage


# Fields that add up across a day. `ml` is here too: water is logged a glass at
# a time and the day's figure is the total, exactly like calories.
_INTAKE_SUMMED = ("kcal", "protein_g", "fat_g", "carbs_g", "ml")


def _daily_intake(entries: list[IntakeEntry]) -> dict[date, dict[str, Any]]:
    """Roll entries up per day.

    The database does the join, so a caller never adds meals together — and an
    agent logging lunch never has to know what breakfast was. That is the whole
    reason entries replaced a daily row.

    A field stays ``None`` when no entry that day carried it, rather than
    becoming 0. Nobody logging only water has eaten zero calories; they have
    logged no calories, and the two must not read the same.
    """
    days: dict[date, dict[str, Any]] = {}
    for entry in entries:
        day = days.setdefault(
            entry.date,
            {"date": entry.date.isoformat(), "entries": 0} | dict.fromkeys(_INTAKE_SUMMED),
        )
        day["entries"] += 1
        for field in _INTAKE_SUMMED:
            value = getattr(entry, field, None)
            if value is not None:
                day[field] = (day[field] or 0) + value
    return days


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #
def get_training_week(week_start: str | None = None) -> dict[str, Any]:
    """The merged week: sessions, health, food, body, and the derived signals."""
    start = _parse_week_start(week_start)
    end = start + timedelta(days=6)
    prior_start, prior_end = start - timedelta(days=7), start - timedelta(days=1)

    with get_session() as session:
        sessions = session.exec(
            select(Activity)
            .where(col(Activity.date) >= start, col(Activity.date) <= end)
            .order_by(col(Activity.date))
        ).all()
        health = session.exec(
            select(DailyHealth)
            .where(col(DailyHealth.date) >= start, col(DailyHealth.date) <= end)
            .order_by(col(DailyHealth.date))
        ).all()
        prior_health = session.exec(
            select(DailyHealth).where(
                col(DailyHealth.date) >= prior_start, col(DailyHealth.date) <= prior_end
            )
        ).all()
        intake = session.exec(
            select(IntakeEntry)
            .where(col(IntakeEntry.date) >= start, col(IntakeEntry.date) <= end)
            .order_by(col(IntakeEntry.at))
        ).all()
        body = session.exec(
            select(Body)
            .where(col(Body.date) >= start, col(Body.date) <= end)
            .order_by(col(Body.date))
        ).all()
        # The most recent measurement *before* this week, so a weight change is
        # measured against the last real reading rather than assumed absent.
        prior_body = session.exec(
            select(Body).where(col(Body.date) < start).order_by(desc(col(Body.date)))
        ).first()

    health_by_date = {row.date: row for row in health}
    food_by_date = _daily_intake(list(intake))
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        h = health_by_date.get(d)
        n = food_by_date.get(d)
        days.append(
            {
                "date": d.isoformat(),
                "health": _dump(h) if h else None,
                "nutrition": n,
            }
        )

    ramp = tss_ramp(start)
    loads = training_load_series(end - timedelta(days=WARMUP_DAYS), end)
    latest_load = loads[-1] if loads else {}

    rhr = _mean([r.resting_hr for r in health])
    rhr_prior = _mean([r.resting_hr for r in prior_health])
    hrv = _mean([r.hrv_ms for r in health])
    hrv_prior = _mean([r.hrv_ms for r in prior_health])
    latest_body = body[-1] if body else None

    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "sessions": [_dump(s) for s in sessions],
        "days": days,
        "body": [_dump(b) for b in body],
        "totals": {
            "sessions": len(sessions),
            "tss": ramp["tss"],
            "duration_h": round(sum(s.duration_s or 0 for s in sessions) / 3600, 2),
            "work_kj": round(sum(s.work_kj or 0 for s in sessions), 1),
        },
        "signals": {
            # Nutrition. Flagged before anything else by the coaching rules.
            # Averaged over days that have an entry, not over the week: a week
            # with two logged days is a sample of two, and dividing by seven
            # would report an under-eating that is really under-logging.
            "avg_kcal": _mean([d["kcal"] for d in food_by_date.values()], 0),
            "avg_protein_g": _mean([d["protein_g"] for d in food_by_date.values()]),
            "avg_fat_g": _mean([d["fat_g"] for d in food_by_date.values()]),
            "avg_carbs_g": _mean([d["carbs_g"] for d in food_by_date.values()]),
            "avg_water_ml": _mean([d["ml"] for d in food_by_date.values()]),
            "nutrition_days_logged": len(food_by_date),
            # Load.
            "tss": ramp["tss"],
            "prior_week_tss": ramp["prior_week_tss"],
            "tss_ramp_pct": ramp["pct"],
            "ctl": latest_load.get("ctl"),
            "atl": latest_load.get("atl"),
            "tsb": latest_load.get("tsb"),
            "sessions_completed": len(sessions),
            # Recovery. The rule needs a direction, so both weeks are reported
            # rather than only the delta.
            "resting_hr_avg": rhr,
            "resting_hr_prior_week_avg": rhr_prior,
            "resting_hr_delta": round(rhr - rhr_prior, 1)
            if rhr is not None and rhr_prior is not None
            else None,
            "hrv_avg": hrv,
            "hrv_prior_week_avg": hrv_prior,
            "hrv_delta": round(hrv - hrv_prior, 1)
            if hrv is not None and hrv_prior is not None
            else None,
            "sleep_score_avg": _mean([r.sleep_score for r in health]),
            "sleep_hours_avg": _mean(
                [r.sleep_duration_s / 3600 for r in health if r.sleep_duration_s is not None]
            ),
            # Body. Waist is the primary measure.
            "weight_kg": latest_body.weight_kg if latest_body else None,
            "waist_cm": latest_body.waist_cm if latest_body else None,
            "weight_change_kg": round(latest_body.weight_kg - prior_body.weight_kg, 2)
            if latest_body
            and prior_body
            and latest_body.weight_kg is not None
            and prior_body.weight_kg is not None
            else None,
            "waist_change_cm": round(latest_body.waist_cm - prior_body.waist_cm, 1)
            if latest_body
            and prior_body
            and latest_body.waist_cm is not None
            and prior_body.waist_cm is not None
            else None,
        },
        # Read this before reading anything into a gap. A sync that died looks
        # exactly like a week of rest days until you check which days are absent.
        "coverage": {
            "health": _health_coverage(list(health_by_date.values()), start, end),
            "nutrition": _gaps(set(food_by_date), start, end),
            # Normally empty. A non-empty list means two vendors both stored one
            # effort and the ingest filter that should have prevented it did
            # not — the load below is right, but the ingestion needs fixing.
            "duplicate_efforts": duplicate_efforts(start, end),
            # Which is the difference between "no ride happened" and "nothing
            # asked Wahoo whether one did". Without it every gap above is
            # ambiguous, and the ambiguity resolves the flattering way.
            "sync": sync_status_by_source(),
        },
    }


def get_daily_series(days: int = 90) -> dict[str, Any]:
    """One row per day, every variable already joined. The correlation substrate.

    The question this exists for is whether fuel and hydration track against
    recovery — and recovery signals are *daily*, which is roughly 365
    observations a year against perhaps 50 for per-ride performance. Shorter
    causal chain, far better sample size.

    Nothing else served that. ``get_training_week`` is one week,
    ``get_health_trend`` is health alone, ``get_recent_activities`` is
    activities alone. Correlating across them would mean joining in a context
    window, which is the thing this project exists to avoid.

    **Every day in the window is present.** A day with no data has explicit
    nulls rather than being absent, because a shorter list silently changes what
    a correlation is computed over.

    ``tss`` is the exception and is 0.0 on a day with no session: a rest day
    genuinely carried no load, and that is a measurement rather than a gap. Read
    ``coverage.activities`` before trusting a run of zeros — a failed sync looks
    exactly like a week off.

    ``weight_kg`` appears only on days it was measured. It is deliberately not
    carried forward; interpolating a body weight invents the very trend someone
    would then read a correlation into.
    """
    end = today()
    start = end - timedelta(days=max(days, 1) - 1)

    # Warmed up from before the window so CTL and ATL are not ramping from cold
    # inside the range being looked at.
    loads = {
        row["date"]: row for row in training_load_series(start - timedelta(days=WARMUP_DAYS), end)
    }

    with get_session() as session:
        health = session.exec(
            select(DailyHealth)
            .where(col(DailyHealth.date) >= start, col(DailyHealth.date) <= end)
            .order_by(col(DailyHealth.date))
        ).all()
        intake = session.exec(
            select(IntakeEntry).where(col(IntakeEntry.date) >= start, col(IntakeEntry.date) <= end)
        ).all()
        body = session.exec(
            select(Body).where(col(Body.date) >= start, col(Body.date) <= end)
        ).all()
        sessions = session.exec(
            select(Activity).where(col(Activity.date) >= start, col(Activity.date) <= end)
        ).all()

    health_by_date = {r.date: r for r in health}
    intake_by_date = _daily_intake(list(intake))
    body_by_date = {r.date: r for r in body}
    session_dates = {a.date for a in sessions}

    rows: list[dict[str, Any]] = []
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        key = day.isoformat()
        load = loads.get(key, {})
        h = health_by_date.get(day)
        food = intake_by_date.get(day, {})
        b = body_by_date.get(day)
        rows.append(
            {
                "date": key,
                "tss": load.get("tss"),
                "ctl": load.get("ctl"),
                "atl": load.get("atl"),
                "tsb": load.get("tsb"),
                "resting_hr": h.resting_hr if h else None,
                "hrv_ms": h.hrv_ms if h else None,
                "hrv_status": h.hrv_status if h else None,
                "sleep_score": h.sleep_score if h else None,
                "sleep_duration_s": h.sleep_duration_s if h else None,
                "body_battery_high": h.body_battery_high if h else None,
                "body_battery_low": h.body_battery_low if h else None,
                "steps": h.steps if h else None,
                "kcal": food.get("kcal"),
                "protein_g": food.get("protein_g"),
                "fat_g": food.get("fat_g"),
                "carbs_g": food.get("carbs_g"),
                "water_ml": food.get("ml"),
                "weight_kg": b.weight_kg if b else None,
                "waist_cm": b.waist_cm if b else None,
            }
        )

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": rows,
        "coverage": {
            "health": _health_coverage(list(health), start, end),
            "intake": _gaps(set(intake_by_date), start, end),
            "activities": _gaps(session_dates, start, end),
            "body": _gaps(set(body_by_date), start, end),
        },
    }


def get_health_trend(days: int = 14) -> dict[str, Any]:
    """Resting HR, HRV, sleep and Body Battery over a window, newest first.

    For spotting the multi-day patterns the coaching rules key on — which need a
    run of days, not a single reading.
    """
    end = today()
    start = end - timedelta(days=days - 1)
    with get_session() as session:
        rows = session.exec(
            select(DailyHealth)
            .where(col(DailyHealth.date) >= start, col(DailyHealth.date) <= end)
            .order_by(desc(col(DailyHealth.date)))
        ).all()

    recent = rows[:3]  # newest first, so this is the last three days with data
    baseline = rows[3:]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": [_dump(r) for r in rows],
        "signals": {
            "resting_hr_3d_avg": _mean([r.resting_hr for r in recent]),
            "resting_hr_baseline_avg": _mean([r.resting_hr for r in baseline]),
            "hrv_3d_avg": _mean([r.hrv_ms for r in recent]),
            "hrv_baseline_avg": _mean([r.hrv_ms for r in baseline]),
            "sleep_score_avg": _mean([r.sleep_score for r in rows]),
            "body_battery_high_avg": _mean([r.body_battery_high for r in rows]),
            "body_battery_low_avg": _mean([r.body_battery_low for r in rows]),
        },
        "coverage": _health_coverage(list(rows), start, end),
    }


def get_recent_activities(n: int = 10, include_raw: bool = False) -> list[dict[str, Any]]:
    """Session detail, newest first, for when one specific ride needs looking at."""
    with get_session() as session:
        rows = session.exec(
            select(Activity)
            .order_by(desc(col(Activity.date)), desc(col(Activity.started_at)))
            .limit(n)
        ).all()
    return [_dump(r, include_raw) for r in rows]


def get_tests() -> list[dict[str, Any]]:
    """FTP and 4DP history, newest first, with W/kg where weight is known."""
    with get_session() as session:
        rows = session.exec(select(FitnessTest).order_by(desc(col(FitnessTest.date)))).all()
    out = []
    for row in rows:
        data = _dump(row)
        data["w_per_kg"] = round(row.ftp / row.weight_kg, 2) if row.ftp and row.weight_kg else None
        out.append(data)
    return out


def _run_dump(row: SyncRun | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = _dump(row)
    data.pop("id", None)
    return data


def _age_s(stamp: datetime | None) -> float | None:
    """Seconds since a stored timestamp, or None if there is none.

    Stored timestamps are naive UTC by convention, so the zone is attached here
    rather than assumed by a subtraction that would raise on the mismatch.
    """
    if stamp is None:
        return None
    return round((now() - stamp.replace(tzinfo=UTC)).total_seconds(), 1)


def _last_run(session: Any, source: str, status: str | None = None) -> SyncRun | None:
    stmt = select(SyncRun).where(col(SyncRun.source) == source)
    if status is not None:
        stmt = stmt.where(col(SyncRun.status) == status)
    rows = session.exec(
        stmt.order_by(desc(col(SyncRun.started_at)), desc(col(SyncRun.id))).limit(1)
    ).all()
    return rows[0] if rows else None


def sync_status_by_source() -> dict[str, dict[str, Any]]:
    """Per source: did ingestion run, when, and did it work.

    Every source in :data:`SYNC_SOURCES` appears, including one that has never
    recorded a run — ``status: "never"`` with explicit nulls. Dropping it would
    make a worker that was never deployed look identical to one that is
    healthy, which is the failure this whole table exists to prevent.

    Ages are reported and not judged. What counts as *too long* differs by
    source — Garmin runs daily, Wahoo hourly — and thresholds belong with the
    coaching rules, in the skill, not compiled in here.
    """
    out: dict[str, dict[str, Any]] = {}
    with get_session() as session:
        for source in SYNC_SOURCES:
            latest = _last_run(session, source)
            success = _last_run(session, source, SYNC_OK)
            out[source] = {
                "status": latest.status if latest else NEVER,
                "last_run_at": latest.started_at.isoformat() if latest else None,
                "last_success_at": success.finished_at.isoformat()
                if success and success.finished_at
                else None,
                "seconds_since_success": _age_s(success.finished_at) if success else None,
                "window_days": latest.window_days if latest else None,
                "counts": dict(latest.counts or {}) if latest else None,
                # Populated only when the last run failed. A stale error from a
                # run three days ago, sitting beside a success from an hour
                # ago, reads as a live problem and is not one.
                "error": latest.error if latest and latest.status != SYNC_OK else None,
            }
    return out


def get_sync_status() -> dict[str, Any]:
    """Whether ingestion is alive, with the last few runs per source.

    The question this answers used to need shell access to the Pi: the workers
    are `while true; sleep` loops in compose, and a loop that dies leaves the
    database looking exactly like a quiet week.
    """
    status = sync_status_by_source()
    with get_session() as session:
        for source in SYNC_SOURCES:
            rows = session.exec(
                select(SyncRun)
                .where(col(SyncRun.source) == source)
                .order_by(desc(col(SyncRun.started_at)), desc(col(SyncRun.id)))
                .limit(MAX_LISTED_RUNS)
            ).all()
            status[source]["recent_runs"] = [_run_dump(r) for r in rows]
    return {"checked_at": now().isoformat(), "sources": status}


# --------------------------------------------------------------------------- #
# writes — narrow and deliberate
# --------------------------------------------------------------------------- #
def _append(entry: IntakeEntry) -> dict[str, Any]:
    """Store one entry and return the day it landed on, totalled.

    Returning the running total rather than the entry is deliberate: the caller
    almost always wants to know where the day now stands, and making them ask
    again would put the arithmetic back in the conversation.
    """
    with get_session() as session:
        session.add(entry)
        session.commit()
        rest = session.exec(select(IntakeEntry).where(col(IntakeEntry.date) == entry.date)).all()
        totals = _daily_intake(list(rest))[entry.date]
    return {"logged": _dump(entry), "day_total": totals}


def log_food(
    kcal: int | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    item: str | None = None,
    qty: float | None = None,
    unit: str | None = None,
    note: str | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Add one thing eaten. Appends — it never overwrites what is already there.

    Call it per meal. Nobody knows their day's total while eating, and the tool
    this replaced required exactly that: it rewrote the whole day, so logging
    lunch erased breakfast and nulled any macro not repeated.

    Estimation belongs in the conversation, not here. Read the label, judge the
    portion, and pass numbers. Supplements are the same shape with ``qty`` and
    ``unit`` — 1.5 scoops, 2 pills — and no macros.

    ``day`` defaults to today in the configured timezone, so an evening meal
    lands on the evening's date.
    """
    target = date.fromisoformat(day) if day else today()
    return _append(
        IntakeEntry(
            at=now().replace(tzinfo=None),
            date=target,
            item=item,
            qty=qty,
            unit=unit,
            kcal=kcal,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            note=note,
        )
    )


def log_water(
    ml: int,
    note: str | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Add water, in millilitres. Appends, so a glass at a time is the point.

    Stored alongside food rather than in its own table: a recovery shake is both
    at once, and a type tag would force it to be one.
    """
    target = date.fromisoformat(day) if day else today()
    return _append(IntakeEntry(at=now().replace(tzinfo=None), date=target, ml=ml, note=note))


def log_body(
    day: str | None = None,
    weight_kg: float | None = None,
    waist_cm: float | None = None,
) -> dict[str, Any]:
    """Record a body measurement. Upserts on the date."""
    target = date.fromisoformat(day) if day else today()
    with get_session() as session:
        session.merge(Body(date=target, weight_kg=weight_kg, waist_cm=waist_cm))
        session.commit()
        return _dump(session.get(Body, target))


def log_test(
    day: str,
    test_type: str,
    ftp: int | None = None,
    map_w: int | None = None,
    ac_w: int | None = None,
    nm_w: int | None = None,
    lthr: int | None = None,
    weight_kg: float | None = None,
    rider_type: str | None = None,
) -> dict[str, Any]:
    """Record a test result.

    Not exposed as an MCP tool: four numbers every six weeks does not justify a
    slot in a six-tool surface. Called from the CLI, and here so the write path
    is the same one everything else uses.
    """
    row = FitnessTest(
        date=date.fromisoformat(day),
        test_type=test_type,
        ftp=ftp,
        map_w=map_w,
        ac_w=ac_w,
        nm_w=nm_w,
        lthr=lthr,
        weight_kg=weight_kg,
        rider_type=rider_type,
    )
    with get_session() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return _dump(row)


# The shortest gap between one source's sync and the next one asked for by
# hand. Not a vendor limit — it is a guard against a caller that asks in a
# loop, which is a real shape for a language model with no memory of its last
# call. On Garmin that costs more than wasted time: its SSO limit is per
# account and only time clears it. Short enough that a person who has just
# stepped off the trainer never meets it.
MIN_REQUEST_GAP_S = 120


def request_sync(source: str = "all", note: str | None = None) -> dict[str, Any]:
    """Ask a sync worker to run now. Queues the ask; does not perform the sync.

    The server cannot sync. It holds no vendor credential and is not allowed to
    hold one — see :mod:`soma.sync_requests` for why. This appends a row the
    vendor's worker is already waiting on, and returns immediately.

    The reply is deliberately not a promise. Each source reports whether an ask
    was queued, and its current health from ``sync_runs`` — because a request
    handed to a worker that died on Tuesday is never served, and the caller
    cannot tell that apart from a slow one without being told.
    """
    if source in ("all", ""):
        sources = list(SYNC_SOURCES)
    elif source in SYNC_SOURCES:
        sources = [source]
    else:
        raise ValueError(f"unknown source {source!r}; expected one of {SYNC_SOURCES} or 'all'")

    health = sync_status_by_source()
    out: dict[str, Any] = {}
    for name in sources:
        out[name] = _request_one(name, note) | {
            "worker": {
                "status": health[name]["status"],
                "last_run_at": health[name]["last_run_at"],
                "seconds_since_success": health[name]["seconds_since_success"],
            }
        }
    return {
        "requested_at": now().isoformat(),
        "poll_seconds": settings.sync_poll_s,
        "sources": out,
    }


def _request_one(source: str, note: str | None) -> dict[str, Any]:
    with get_session() as session:
        latest = _last_run(session, source)
    since = _age_s(latest.started_at) if latest else None
    if since is not None and since < MIN_REQUEST_GAP_S:
        return {
            "queued": False,
            "reason": f"{source} synced {int(since)}s ago; the minimum gap is {MIN_REQUEST_GAP_S}s",
            "requested_at": None,
        }

    row, created = sync_requests.enqueue(source, note=note)
    return {
        "queued": True,
        "reason": "queued" if created else "an unserved request was already waiting",
        "requested_at": row.requested_at.isoformat(),
    }


__all__ = [
    "get_daily_series",
    "get_health_trend",
    "get_recent_activities",
    "get_tests",
    "get_training_week",
    "log_body",
    "log_food",
    "log_test",
    "log_water",
    "request_sync",
]
