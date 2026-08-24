"""get_training_week — the join the whole system exists to do.

One call has to return rides, sleep, HRV, food and body already correlated by
date, plus the derived numbers the coaching rules read. If this is wrong the
analysis layer is reasoning over a lie, so it is tested field by field.
"""

from __future__ import annotations

from datetime import timedelta

from traindb.serve.queries import get_training_week

# --- shape -----------------------------------------------------------------


def test_resolves_to_the_containing_monday(seeded):
    # Any date inside the week should work, so the caller never needs a calendar.
    wednesday = seeded["days"][2].isoformat()
    assert get_training_week(wednesday)["week_start"] == seeded["monday"].isoformat()


def test_defaults_to_the_current_week(db):
    from datetime import date

    week = get_training_week()
    assert week["week_start"] == (date.today() - timedelta(days=date.today().weekday())).isoformat()


def test_always_returns_seven_days(seeded):
    # Including the four with no data — an absent day must be visible as a null,
    # not as a shorter list.
    assert len(get_training_week(seeded["monday"].isoformat())["days"]) == 7


def test_days_that_have_no_data_are_explicit_nulls(seeded):
    days = get_training_week(seeded["monday"].isoformat())["days"]
    assert days[6]["health"] is None
    assert days[6]["nutrition"] is None


def test_health_and_food_land_on_the_same_day(seeded):
    # The join, in one assertion.
    day = get_training_week(seeded["monday"].isoformat())["days"][0]
    assert day["health"]["resting_hr"] == 50
    assert day["nutrition"]["kcal"] == 2_400


def test_raw_payloads_are_not_shipped(seeded):
    day = get_training_week(seeded["monday"].isoformat())["days"][0]
    assert "raw" not in day["health"]


# --- sessions and totals ---------------------------------------------------


def test_only_this_week_s_sessions_are_included(seeded):
    week = get_training_week(seeded["monday"].isoformat())
    assert [s["external_id"] for s in week["sessions"]] == ["1001", "1002"]


def test_totals_sum_the_week(seeded):
    totals = get_training_week(seeded["monday"].isoformat())["totals"]
    assert totals["sessions"] == 2
    assert totals["tss"] == 200.0
    assert totals["duration_h"] == 1.83
    assert totals["work_kj"] == 1_248.0


# --- signals ---------------------------------------------------------------


def test_nutrition_averages_ignore_unlogged_days(seeded):
    # Two days logged out of seven. A day with no entry is unknown intake, not a
    # fast, so it must not drag the average toward zero.
    signals = get_training_week(seeded["monday"].isoformat())["signals"]
    assert signals["avg_kcal"] == 2_300
    assert signals["avg_protein_g"] == 145.0
    assert signals["nutrition_days_logged"] == 2


def test_ramp_is_a_ratio_against_the_prior_week(seeded):
    signals = get_training_week(seeded["monday"].isoformat())["signals"]
    assert signals["prior_week_tss"] == 100.0
    assert signals["tss_ramp_pct"] == 100.0


def test_resting_hr_delta_compares_the_two_weeks(seeded):
    signals = get_training_week(seeded["monday"].isoformat())["signals"]
    assert signals["resting_hr_avg"] == 51.0
    assert signals["resting_hr_prior_week_avg"] == 48.0
    assert signals["resting_hr_delta"] == 3.0


def test_hrv_delta_can_be_negative(seeded):
    # The rule that matters pairs a rising resting HR with a falling HRV, so the
    # sign has to survive.
    assert get_training_week(seeded["monday"].isoformat())["signals"]["hrv_delta"] == -4.0


def test_sleep_averages(seeded):
    signals = get_training_week(seeded["monday"].isoformat())["signals"]
    assert signals["sleep_score_avg"] == 81.0
    assert signals["sleep_hours_avg"] == 7.5


def test_body_change_measures_against_the_last_reading_before_the_week(seeded):
    signals = get_training_week(seeded["monday"].isoformat())["signals"]
    assert signals["weight_kg"] == 72.0
    assert signals["weight_change_kg"] == -0.8
    assert signals["waist_change_cm"] == -1.0


def test_load_series_is_reported(seeded):
    signals = get_training_week(seeded["monday"].isoformat())["signals"]
    assert signals["ctl"] is not None
    assert signals["atl"] is not None
    assert signals["tsb"] is not None


def test_signals_are_null_rather_than_zero_on_an_empty_week(db):
    signals = get_training_week()["signals"]
    assert signals["avg_kcal"] is None
    assert signals["resting_hr_avg"] is None
    assert signals["weight_change_kg"] is None


def test_ramp_from_a_week_with_no_load_reports_none(db):
    # Not "infinite growth" — a first week back. Reporting a number here would
    # trip the ramp rule on the one week it should not apply to.
    assert get_training_week()["signals"]["tss_ramp_pct"] is None


# --- coverage --------------------------------------------------------------


def test_coverage_counts_present_and_missing_days(seeded):
    coverage = get_training_week(seeded["monday"].isoformat())["coverage"]
    assert coverage["health"]["days_present"] == 3
    assert coverage["health"]["days_missing"] == 4
    assert coverage["nutrition"]["days_present"] == 2


def test_coverage_names_the_missing_days(seeded):
    # Which days is the point: it tells a failed sync apart from a rest day.
    coverage = get_training_week(seeded["monday"].isoformat())["coverage"]
    missing = coverage["health"]["missing"]
    assert (seeded["monday"] + timedelta(days=6)).isoformat() in missing
    assert seeded["monday"].isoformat() not in missing


def test_coverage_on_an_empty_week_reports_nothing_present(db):
    assert get_training_week()["coverage"]["health"]["days_present"] == 0
