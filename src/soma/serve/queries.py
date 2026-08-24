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

from datetime import date, timedelta
from typing import Any

from sqlmodel import col, desc, select

from soma.clock import today
from soma.db import get_session
from soma.metrics import duplicate_efforts, training_load_series, tss_ramp
from soma.models import WATCH_DERIVED_FIELDS, Activity, Body, DailyHealth, FitnessTest, Nutrition

# How far back to warm the CTL/ATL EWMAs before reading them. CTL's time
# constant is 42 days, so a shorter window reports fitness that is an artefact
# of where the window started.
WARMUP_DAYS = 120

# Coverage gaps are listed by date rather than counted, because "which days"
# is what tells a failed sync apart from a rest day. Long backfills can be
# missing hundreds, so the list is capped and the true count sent alongside.
MAX_LISTED_GAPS = 14


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
        food = session.exec(
            select(Nutrition)
            .where(col(Nutrition.date) >= start, col(Nutrition.date) <= end)
            .order_by(col(Nutrition.date))
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
    food_by_date = {row.date: row for row in food}
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        h = health_by_date.get(d)
        n = food_by_date.get(d)
        days.append(
            {
                "date": d.isoformat(),
                "health": _dump(h) if h else None,
                "nutrition": _dump(n) if n else None,
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
            "avg_kcal": _mean([n.kcal for n in food], 0),
            "avg_protein_g": _mean([n.protein_g for n in food]),
            "avg_fat_g": _mean([n.fat_g for n in food]),
            "avg_carbs_g": _mean([n.carbs_g for n in food]),
            "nutrition_days_logged": len(food),
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


# --------------------------------------------------------------------------- #
# writes — narrow and deliberate
# --------------------------------------------------------------------------- #
def log_nutrition(
    day: str | None = None,
    kcal: int | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    creatine: bool = False,
    magnesium: bool = False,
    vitamin_d: bool = False,
    probiotic: bool = False,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a day's intake. Upserts on the date — no delete, corrections overwrite."""
    target = date.fromisoformat(day) if day else today()
    row = Nutrition(
        date=target,
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        creatine=creatine,
        magnesium=magnesium,
        vitamin_d=vitamin_d,
        probiotic=probiotic,
        note=note,
    )
    with get_session() as session:
        session.merge(row)
        session.commit()
        stored = session.get(Nutrition, target)
        return _dump(stored)


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


__all__ = [
    "get_health_trend",
    "get_recent_activities",
    "get_tests",
    "get_training_week",
    "log_body",
    "log_nutrition",
    "log_test",
]
