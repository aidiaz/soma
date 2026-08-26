"""The correlation substrate.

Every day in the window must be present. A shorter list quietly changes what a
correlation is computed over, which is the failure this whole shape exists to
prevent — worse than a wrong number, because nothing looks wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from soma.clock import today
from soma.db import get_session
from soma.models import Activity, Body, DailyHealth, IntakeEntry
from soma.serve.queries import get_daily_series


def test_every_day_in_the_window_is_present(db):
    series = get_daily_series(days=30)
    assert len(series["days"]) == 30
    dates = [d["date"] for d in series["days"]]
    assert dates == sorted(dates), "days must be ordered"
    assert dates[-1] == today().isoformat()


def test_a_day_with_no_data_is_nulls_not_omitted(db):
    day = get_daily_series(days=7)["days"][0]
    for field in ("resting_hr", "hrv_ms", "sleep_score", "kcal", "protein_g", "water_ml"):
        assert day[field] is None, f"{field} should be null on a day with nothing recorded"


def test_a_rest_day_is_zero_tss_not_null(db):
    # A rest day genuinely carried no load. That is a measurement, and turning
    # it into a null would make the EWMA and any correlation skip the day.
    assert get_daily_series(days=7)["days"][0]["tss"] == 0.0


def test_intake_is_summed_per_day(db):
    d = today()
    with get_session() as s:
        for kcal, protein in ((500, 30.0), (700, 20.0)):
            s.add(
                IntakeEntry(
                    at=datetime.combine(d, datetime.min.time()),
                    date=d,
                    kcal=kcal,
                    protein_g=protein,
                )
            )
        s.commit()
    row = get_daily_series(days=2)["days"][-1]
    assert row["kcal"] == 1_200
    assert row["protein_g"] == 50.0


def test_water_appears_without_implying_food(db):
    d = today()
    with get_session() as s:
        s.add(IntakeEntry(at=datetime.combine(d, datetime.min.time()), date=d, ml=500))
        s.commit()
    row = get_daily_series(days=2)["days"][-1]
    assert row["water_ml"] == 500
    assert row["kcal"] is None, "logging water is not eating zero calories"


def test_weight_is_not_carried_forward(db):
    """Interpolating bodyweight invents the trend a correlation would read."""
    d = today()
    with get_session() as s:
        s.merge(Body(date=d - timedelta(days=3), weight_kg=71.4))
        s.commit()
    rows = {r["date"]: r for r in get_daily_series(days=7)["days"]}
    assert rows[(d - timedelta(days=3)).isoformat()]["weight_kg"] == 71.4
    assert rows[(d - timedelta(days=2)).isoformat()]["weight_kg"] is None
    assert rows[d.isoformat()]["weight_kg"] is None


def test_health_and_intake_land_on_the_same_row(db):
    """The point of the tool: one row, both variables, joined by the database."""
    d = today()
    with get_session() as s:
        s.merge(DailyHealth(date=d, resting_hr=51, hrv_ms=57, sleep_score=85))
        s.add(IntakeEntry(at=datetime.combine(d, datetime.min.time()), date=d, ml=2_000))
        s.commit()
    row = get_daily_series(days=2)["days"][-1]
    assert (row["hrv_ms"], row["sleep_score"], row["water_ml"]) == (57, 85, 2_000)


def test_coverage_distinguishes_each_source(db):
    d = today()
    with get_session() as s:
        s.merge(DailyHealth(date=d, resting_hr=51))
        s.add(
            Activity(
                source="test",
                external_id="a1",
                date=d,
                tss=50.0,
                started_at=datetime.combine(d, datetime.min.time()),
            )
        )
        s.commit()
    cov = get_daily_series(days=5)["coverage"]
    assert cov["health"]["days_present"] == 1
    assert cov["activities"]["days_present"] == 1
    assert cov["intake"]["days_present"] == 0, "nothing was logged, and that must show"
    assert cov["body"]["days_present"] == 0
