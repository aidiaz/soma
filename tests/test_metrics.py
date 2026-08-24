"""CTL / ATL / TSB and the weekly ramp."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from traindb.db import get_session
from traindb.metrics import (
    ATL_TAU,
    CTL_TAU,
    _alpha,
    daily_tss,
    training_load_series,
    tss_ramp,
    week_tss,
)
from traindb.models import Activity

START = date(2026, 1, 5)  # a Monday


def _add(session, activity_id, day, tss, hour=8):
    session.merge(
        Activity(
            id=activity_id,
            source="test",
            external_id=str(activity_id),
            started_at=datetime.combine(day, datetime.min.time()).replace(hour=hour),
            date=day,
            tss=tss,
            raw={},
        )
    )


def test_alpha_matches_the_ewma_definition():
    assert _alpha(42) == 1.0 - math.exp(-1.0 / 42)


def test_ctl_reacts_more_slowly_than_atl():
    # Fitness is the 42-day average, fatigue the 7-day one.
    assert _alpha(CTL_TAU) < _alpha(ATL_TAU)


def test_daily_tss_zero_fills_rest_days(db):
    # The EWMA has to decay through rest days; a missing key would skip one.
    loads = daily_tss(START, START + timedelta(days=4))
    assert len(loads) == 5
    assert set(loads.values()) == {0.0}


def test_daily_tss_sums_two_sessions_on_one_day(db):
    with get_session() as session:
        _add(session, 1, START, 100.0, hour=7)
        _add(session, 2, START, 50.0, hour=18)
        session.commit()
    assert daily_tss(START, START)[START] == 150.0


def test_daily_tss_ignores_sessions_outside_the_window(db):
    with get_session() as session:
        _add(session, 1, START - timedelta(days=1), 999.0)
        session.commit()
    assert daily_tss(START, START)[START] == 0.0


def test_daily_tss_ignores_sessions_with_no_tss(db):
    with get_session() as session:
        _add(session, 1, START, None)
        session.commit()
    assert daily_tss(START, START)[START] == 0.0


def test_series_has_one_entry_per_day(db):
    series = training_load_series(START, START + timedelta(days=6))
    assert [row["date"] for row in series] == [
        (START + timedelta(days=i)).isoformat() for i in range(7)
    ]


def test_series_starts_cold_at_zero(db):
    first = training_load_series(START, START)[0]
    assert (first["ctl"], first["atl"], first["tsb"]) == (0.0, 0.0, 0.0)


def test_a_hard_day_raises_fatigue_faster_than_fitness(db):
    with get_session() as session:
        _add(session, 1, START, 200.0)
        session.commit()
    day = training_load_series(START, START)[0]
    assert day["atl"] > day["ctl"]


def test_tsb_reflects_the_previous_day(db):
    with get_session() as session:
        _add(session, 1, START, 200.0)
        session.commit()
    series = training_load_series(START, START + timedelta(days=1))
    assert series[1]["tsb"] == round(series[0]["ctl"] - series[0]["atl"], 1)


def test_rest_decays_fatigue(db):
    with get_session() as session:
        _add(session, 1, START, 300.0)
        session.commit()
    series = training_load_series(START, START + timedelta(days=10))
    assert series[-1]["atl"] < series[0]["atl"]


def test_seeds_carry_prior_fitness_in(db):
    cold = training_load_series(START, START)[0]
    warm = training_load_series(START, START, seed_ctl=50.0, seed_atl=40.0)[0]
    assert warm["ctl"] > cold["ctl"]
    assert warm["tsb"] == 10.0


# --- ramp ------------------------------------------------------------------


def test_week_tss_sums_seven_days(db):
    with get_session() as session:
        _add(session, 1, START, 100.0)
        _add(session, 2, START + timedelta(days=6), 50.0)
        _add(session, 3, START + timedelta(days=7), 999.0)  # next week
        session.commit()
    assert week_tss(START) == 150.0


def test_ramp_reports_a_percentage(db):
    with get_session() as session:
        _add(session, 1, START - timedelta(days=7), 100.0)
        _add(session, 2, START, 110.0)
        session.commit()
    assert tss_ramp(START)["pct"] == 10.0


def test_ramp_can_be_negative(db):
    with get_session() as session:
        _add(session, 1, START - timedelta(days=7), 200.0)
        _add(session, 2, START, 100.0)
        session.commit()
    assert tss_ramp(START)["pct"] == -50.0


def test_ramp_from_zero_is_none_not_infinity(db):
    # A first week back is not infinite growth, and reporting a number here
    # would trip the +10% rule on the one week it must not apply to.
    with get_session() as session:
        _add(session, 1, START, 100.0)
        session.commit()
    assert tss_ramp(START)["pct"] is None


def test_ramp_is_scale_invariant(db):
    # Why mixing Garmin's EPOC-derived load with Wahoo's TSS is survivable for
    # the ramp specifically: doubling both weeks leaves the ratio unchanged.
    with get_session() as session:
        _add(session, 1, START - timedelta(days=7), 100.0)
        _add(session, 2, START, 150.0)
        session.commit()
    small = tss_ramp(START)["pct"]

    with get_session() as session:
        _add(session, 1, START - timedelta(days=7), 200.0)
        _add(session, 2, START, 300.0)
        session.commit()
    assert tss_ramp(START)["pct"] == small
