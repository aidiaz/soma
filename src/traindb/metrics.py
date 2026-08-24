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
of Garmin numbers compared against a week of Wahoo ones would produce a ramp
that is an artefact of the switch. See the open question in CLAUDE.md.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from sqlmodel import col, select

from traindb.db import get_session
from traindb.models import Activity

CTL_TAU = 42
ATL_TAU = 7


def _alpha(tau: int) -> float:
    return 1.0 - math.exp(-1.0 / tau)


def daily_tss(start: date, end: date) -> dict[date, float]:
    """Sum activity ``tss`` per calendar day across [start, end].

    Days with no session are present with 0.0 rather than absent — the EWMA
    needs rest days to decay through, and a missing key would silently skip one.
    """
    loads: dict[date, float] = {
        start + timedelta(days=i): 0.0 for i in range((end - start).days + 1)
    }
    with get_session() as session:
        rows = session.exec(
            select(Activity).where(col(Activity.date) >= start, col(Activity.date) <= end)
        ).all()
    for act in rows:
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
