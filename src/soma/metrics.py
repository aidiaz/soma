"""Derived training-load metrics, computed from stored activities.

CTL / ATL / TSB follow the standard exponentially-weighted moving-average
(Banister / TrainingPeaks) model over daily training stress:

    value_today = value_yesterday + (load_today - value_yesterday) * alpha
    alpha = 1 - exp(-1 / tau)

- CTL (Chronic Training Load, "Fitness"):  tau = 42 days
- ATL (Acute Training Load,   "Fatigue"):  tau = 7  days
- TSB (Training Stress Balance, "Form"):   yesterday's CTL - yesterday's ATL

**On the units.** Wahoo reports true TSS. Garmin reports an EPOC-derived
training load, which is a different quantity on a similar scale. Both land in
``activities.tss``. That is tolerable for the ramp, which is a *ratio* and so
survives any consistent scale, but it is not tolerable across a boundary: a week
of Garmin numbers compared against a week of Wahoo ones produces a ramp that is
an artefact of the switch rather than of training.

Sources therefore own separate domains — Wahoo the virtual rides, Garmin the
outdoor and everything health — so the two quantities sit side by side rather
than being averaged into a number that means neither.
"""

from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Any

from sqlmodel import col, select

from soma.db import get_session
from soma.models import Activity

log = logging.getLogger("soma.metrics")

CTL_TAU = 42
ATL_TAU = 7

# One effort can be stored twice. `models.Activity` says so plainly: uniqueness
# is on (source, external_id), so the same ride arriving from two vendors is two
# rows describing one effort. Summing both inflates CTL and reports a week
# harder than the one that happened — silently, which is worse than a crash,
# because the number that drives coaching is simply wrong.
#
# The primary defence is at ingest: sources own separate domains and garmin-sync
# excludes SYSTM-originated rides. This is the safety net for when that filter
# is wrong, so the matching is deliberately conservative. A false match discards
# real training; a missed match only leaves the original inflation, which the
# warning below makes visible.
#
# Distance is not used. Indoor trainer rides do not have one worth comparing.
DUPLICATE_START_TOLERANCE_S = 20 * 60
DUPLICATE_DURATION_TOLERANCE = 0.05
DUPLICATE_DURATION_FLOOR_S = 60.0

# Which row survives a match. Wahoo reports true TSS and owns virtual rides, so
# it wins where both exist. Anything unlisted sorts last.
SOURCE_PRECEDENCE = ("wahoo", "garmin")


def _precedence(source: str) -> int:
    return (
        SOURCE_PRECEDENCE.index(source) if source in SOURCE_PRECEDENCE else len(SOURCE_PRECEDENCE)
    )


def _same_effort(a: Activity, b: Activity) -> bool:
    """Whether two rows from different sources describe one effort.

    Requires *both* a close start time and a similar duration, and refuses to
    guess when either is missing. Two genuinely different sessions can start
    within the window — a warm-up logged separately, say — but will not also
    match on duration.

    Known limitation: this compares `started_at` directly, and those are naive
    wall-clock readings kept per vendor. If two vendors disagree about the zone
    they report, the timestamps differ by hours and nothing matches here. That
    is not fixable without real Wahoo payloads to look at, and it is the reason
    the ingest-side exclusion is the primary defence rather than this.
    """
    if a.source == b.source:
        return False
    if a.started_at is None or b.started_at is None:
        return False
    if a.duration_s is None or b.duration_s is None:
        return False
    if abs((a.started_at - b.started_at).total_seconds()) > DUPLICATE_START_TOLERANCE_S:
        return False
    longest = max(a.duration_s, b.duration_s, DUPLICATE_DURATION_FLOOR_S)
    return abs(a.duration_s - b.duration_s) / longest <= DUPLICATE_DURATION_TOLERANCE


def _dedupe(rows: list[Activity]) -> tuple[list[Activity], list[dict[str, Any]]]:
    """Split rows into the ones that count and the duplicates they displaced."""
    kept: list[Activity] = []
    dropped: list[dict[str, Any]] = []
    for act in sorted(rows, key=lambda r: (_precedence(r.source), r.source, r.external_id)):
        match = next((k for k in kept if _same_effort(k, act)), None)
        if match is None:
            kept.append(act)
            continue
        dropped.append(
            {
                "date": act.date.isoformat(),
                "kept": {"source": match.source, "external_id": match.external_id},
                "dropped": {"source": act.source, "external_id": act.external_id},
                "tss_not_counted": act.tss,
            }
        )
    return kept, dropped


def _fetch(start: date, end: date) -> list[Activity]:
    with get_session() as session:
        return list(
            session.exec(
                select(Activity).where(col(Activity.date) >= start, col(Activity.date) <= end)
            ).all()
        )


def duplicate_efforts(start: date, end: date) -> list[dict[str, Any]]:
    """Efforts stored twice in [start, end], and which copy was discarded.

    Surfaced in `get_training_week`'s coverage rather than only logged, because
    the thing this protects against is an ingest filter quietly going wrong, and
    a log line on a Pi is not somewhere anyone looks.
    """
    return _dedupe(_fetch(start, end))[1]


def _alpha(tau: int) -> float:
    return 1.0 - math.exp(-1.0 / tau)


def daily_tss(start: date, end: date) -> dict[date, float]:
    """Sum activity ``tss`` per calendar day across [start, end].

    One effort counts once, even when two vendors both stored it. See
    ``_same_effort`` for what counts as the same effort and why the test is
    deliberately strict.

    Days with no session are present with 0.0 rather than absent — the EWMA
    needs rest days to decay through, and a missing key would silently skip one.
    """
    loads: dict[date, float] = {
        start + timedelta(days=i): 0.0 for i in range((end - start).days + 1)
    }
    kept, dropped = _dedupe(_fetch(start, end))
    if dropped:
        # WARNING rather than INFO: reaching here means the ingest-side filter
        # let an overlap through, and that is a bug to fix at the source.
        log.warning(
            "%d duplicate effort(s) excluded from training load: %s",
            len(dropped),
            ", ".join(f"{d['dropped']['source']}:{d['dropped']['external_id']}" for d in dropped),
        )
    for act in kept:
        if act.tss is not None:
            loads[act.date] += act.tss
    return loads


def training_load_series(
    start: date,
    end: date,
    seed_ctl: float = 0.0,
    seed_atl: float = 0.0,
) -> list[dict[str, Any]]:
    """A day-by-day CTL/ATL/TSB series for [start, end].

    ``seed_ctl`` / ``seed_atl`` carry fitness and fatigue in from before the
    window. Left at zero the series ramps from cold, and early values read low
    until the EWMAs warm up over roughly six weeks.
    """
    loads = daily_tss(start, end)
    a_ctl, a_atl = _alpha(CTL_TAU), _alpha(ATL_TAU)
    ctl, atl = seed_ctl, seed_atl
    series: list[dict[str, Any]] = []
    for day in sorted(loads):
        load = loads[day]
        tsb = ctl - atl  # form reflects yesterday's balance
        ctl += (load - ctl) * a_ctl
        atl += (load - atl) * a_atl
        series.append(
            {
                "date": day.isoformat(),
                "tss": round(load, 1),
                "ctl": round(ctl, 1),
                "atl": round(atl, 1),
                "tsb": round(tsb, 1),
            }
        )
    return series


def week_tss(week_start: date) -> float:
    """Total TSS for the seven days beginning ``week_start``."""
    return round(sum(daily_tss(week_start, week_start + timedelta(days=6)).values()), 1)


def tss_ramp(week_start: date) -> dict[str, Any]:
    """This week's TSS against the week before, as a percentage change.

    Returns ``pct: None`` when the prior week has no load at all. A ramp from
    zero is not "infinite growth", it is a first week back, and reporting a
    number there would trip the +10% rule on the one week it should not apply
    to.
    """
    this_week = week_tss(week_start)
    prior = week_tss(week_start - timedelta(days=7))
    pct = None if prior == 0 else round((this_week - prior) / prior * 100, 1)
    return {"tss": this_week, "prior_week_tss": prior, "pct": pct}
