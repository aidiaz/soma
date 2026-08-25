"""The other four tools: health trend, recent activities, tests, and the writes."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlmodel import select

from soma.clock import today as utc_today
from soma.db import get_session
from soma.models import Body, DailyHealth, IntakeEntry
from soma.serve.queries import (
    get_health_trend,
    get_recent_activities,
    get_tests,
    log_body,
    log_food,
    log_test,
    log_water,
)

# --- get_health_trend ------------------------------------------------------


def test_health_trend_returns_newest_first(seeded):
    days = get_health_trend(days=30)["days"]
    assert days[0]["date"] > days[-1]["date"]


def test_health_trend_window_excludes_older_rows(seeded):
    with get_session() as session:
        session.merge(DailyHealth(date=utc_today() - timedelta(days=200), resting_hr=99, raw={}))
        session.commit()
    assert all(row["resting_hr"] != 99 for row in get_health_trend(days=30)["days"])


def test_health_trend_splits_recent_from_baseline(seeded):
    # The recovery rules need three days against a longer baseline, not one
    # reading against another.
    signals = get_health_trend(days=30)["signals"]
    assert signals["resting_hr_3d_avg"] is not None
    assert signals["hrv_3d_avg"] is not None


def test_health_trend_reports_coverage(seeded):
    coverage = get_health_trend(days=30)["coverage"]
    assert coverage["days_present"] == 4
    assert coverage["days_missing"] == 26


def test_health_trend_on_an_empty_database(db):
    trend = get_health_trend(days=7)
    assert trend["days"] == []
    assert trend["signals"]["resting_hr_3d_avg"] is None


# --- get_recent_activities -------------------------------------------------


def test_recent_activities_newest_first(seeded):
    assert [a["external_id"] for a in get_recent_activities()] == ["1002", "1001", "1000"]


def test_recent_activities_respects_the_limit(seeded):
    assert len(get_recent_activities(n=1)) == 1


def test_recent_activities_hides_raw_by_default(seeded):
    assert "raw" not in get_recent_activities()[0]


def test_recent_activities_can_include_raw(seeded):
    assert "raw" in get_recent_activities(include_raw=True)[0]


def test_recent_activities_carries_the_source(seeded):
    # Two sources will share this table, so every row has to say which it is.
    assert get_recent_activities()[0]["source"] == "garmin"


# --- get_tests -------------------------------------------------------------


def test_tests_are_empty_until_one_is_logged(db):
    assert get_tests() == []


def test_test_records_round_trip(db):
    log_test("2026-03-01", "4dp", ftp=250, map_w=340, ac_w=520, nm_w=900, weight_kg=72.0)
    stored = get_tests()[0]
    assert (stored["ftp"], stored["map_w"], stored["nm_w"]) == (250, 340, 900)


def test_tests_report_w_per_kg(db):
    log_test("2026-03-01", "ftp", ftp=252, weight_kg=72.0)
    assert get_tests()[0]["w_per_kg"] == 3.5


def test_w_per_kg_is_none_without_a_weight(db):
    log_test("2026-03-01", "ftp", ftp=250)
    assert get_tests()[0]["w_per_kg"] is None


def test_tests_are_newest_first(db):
    log_test("2026-01-01", "ftp", ftp=240)
    log_test("2026-03-01", "ftp", ftp=250)
    assert [t["date"] for t in get_tests()] == ["2026-03-01", "2026-01-01"]


def test_tests_keep_history_rather_than_overwriting(db):
    # Unlike the day-keyed tables, two tests on different dates are two rows —
    # the history is the scoreboard.
    log_test("2026-01-01", "ftp", ftp=240)
    log_test("2026-03-01", "ftp", ftp=250)
    assert len(get_tests()) == 2


# --- log_food / log_water --------------------------------------------------


def test_log_food_persists(db):
    log_food(kcal=900, protein_g=50.0, day="2026-03-14")
    with get_session() as session:
        rows = session.exec(select(IntakeEntry)).all()
    assert [r.kcal for r in rows] == [900]


def test_a_second_call_adds_rather_than_replacing(db):
    """The bug this shape exists to remove.

    The daily row merged a fully-built object, so a second call overwrote the
    first and nulled every field it did not repeat: breakfast then lunch left
    kcal=700 and protein_g=None.
    """
    log_food(kcal=500, protein_g=30.0, day="2026-03-14")
    result = log_food(kcal=700, day="2026-03-14")
    assert result["day_total"]["kcal"] == 1_200
    assert result["day_total"]["protein_g"] == 30.0, "the earlier macro must survive"


def test_a_day_with_no_calories_logged_reports_none_not_zero(db):
    # Logging only water is not a day of eating nothing.
    result = log_water(ml=500, day="2026-03-14")
    assert result["day_total"]["ml"] == 500
    assert result["day_total"]["kcal"] is None


def test_log_water_accumulates(db):
    log_water(ml=500, day="2026-03-14")
    assert log_water(ml=250, day="2026-03-14")["day_total"]["ml"] == 750


def test_a_supplement_is_an_entry_with_a_quantity(db):
    stored = log_food(item="creatine", qty=1.5, unit="scoop", day="2026-03-14")
    assert stored["logged"]["item"] == "creatine"
    assert stored["logged"]["qty"] == 1.5
    assert stored["day_total"]["kcal"] is None, "a supplement is not food energy"


def test_log_food_defaults_to_today(db):
    assert log_food(kcal=2_000)["logged"]["date"] == utc_today().isoformat()


def test_log_food_accepts_a_note(db):
    stored = log_food(kcal=100, note="ate out, estimated", day="2026-03-14")
    assert stored["logged"]["note"] == "ate out, estimated"


def test_log_food_rejects_a_malformed_date(db):
    with pytest.raises(ValueError):
        log_food(kcal=2_000, day="14-03-2026")


# --- log_body --------------------------------------------------------------


def test_log_body_persists(db):
    log_body("2026-03-14", weight_kg=72.0, waist_cm=84.0)
    with get_session() as session:
        row = session.get(Body, date(2026, 3, 14))
    assert (row.weight_kg, row.waist_cm) == (72.0, 84.0)


def test_log_body_defaults_to_today(db):
    assert log_body(weight_kg=72.0)["date"] == utc_today().isoformat()


def test_log_body_upserts_on_the_date(db):
    log_body("2026-03-14", weight_kg=72.0)
    log_body("2026-03-14", weight_kg=71.5)
    with get_session() as session:
        assert session.get(Body, date(2026, 3, 14)).weight_kg == 71.5


def test_log_body_accepts_waist_alone(db):
    # Waist is the primary measure for the first weeks; weight can be absent.
    assert log_body("2026-03-14", waist_cm=84.0)["weight_kg"] is None
