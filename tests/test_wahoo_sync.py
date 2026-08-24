"""Mapping Wahoo workouts into activities.

The fixtures here are shaped like real Wahoo responses, which matters more than
usual for one reason: **Wahoo returns numerics as strings.** A fixture that used
floats would pass while production silently wrote nulls.
"""

from __future__ import annotations

import datetime as dt

import pytest

from soma.ingest.wahoo.sync import SOURCE, _num, _sport, map_workout


def workout(**overrides):
    """A completed ride, with Wahoo's string numerics preserved."""
    summary = {
        "id": 437189847,
        "started_at": "2026-08-24T16:26:15.000Z",
        "time_zone": "America/Santiago",
        "power_bike_tss_last": "52.3",
        "power_bike_np_last": "139.0",
        "power_avg": "125.0",
        "heart_rate_avg": "145.0",
        "duration_active_accum": "2924.74",
        "work_accum": "365556.0",
    }
    summary.update(overrides.pop("summary", {}))
    base = {
        "id": 490002584,
        "name": "On Location - Catalunya: Costa Brava Coastline",
        "starts": "2026-08-24T16:26:15.000Z",
        "workout_type_id": 12,
        "workout_token": "SYSTM fqf3lvHDvT:0",
        "workout_summary": summary,
    }
    base.update(overrides)
    return base


def test_string_numerics_are_coerced():
    # The regression this file exists for. Wahoo sends "52.3", not 52.3.
    row = map_workout(workout())
    assert row is not None
    assert row.tss == 52.3
    assert row.np == 139.0
    assert row.avg_power == 125.0
    assert row.avg_hr == 145.0
    assert row.duration_s == 2924.74


def test_work_is_converted_from_joules_to_kilojoules():
    # This is the field that actually broke: a units conversion guarded by an
    # isinstance check rejected the string and wrote None.
    row = map_workout(workout())
    assert row is not None
    assert row.work_kj == pytest.approx(365.556)


def test_started_at_is_local_wall_clock_not_utc():
    # 16:26 UTC is 12:26 in Santiago. The date is derived from this, and a
    # 23:30 ride must count towards its own training day.
    row = map_workout(workout())
    assert row is not None
    # Naive on purpose: Activity.date is derived from this wall clock, so that
    # a late ride counts towards its own training day. See _local_wall_clock.
    assert row.started_at == dt.datetime(2026, 8, 24, 12, 26, 15)  # noqa: DTZ001
    assert row.date == dt.date(2026, 8, 24)


def test_a_late_ride_stays_on_its_own_day():
    # 02:30 UTC on the 25th is 22:30 on the 24th in Santiago. Storing UTC here
    # would move a Monday-evening session onto Tuesday.
    row = map_workout(workout(summary={"started_at": "2026-08-25T02:30:00.000Z"}))
    assert row is not None
    assert row.date == dt.date(2026, 8, 24)
    assert row.started_at.hour == 22


def test_scheduled_workouts_are_not_written():
    # Wahoo returns planned sessions from the same endpoint as ridden ones.
    # Writing them would invent training that never happened.
    assert map_workout(workout(workout_summary=None)) is None
    assert (
        map_workout({"id": 1, "name": "Recovery Booster", "starts": "2026-08-30T00:00:00.000Z"})
        is None
    )


def test_a_workout_without_a_start_is_skipped():
    assert map_workout(workout(summary={"started_at": None}, starts=None)) is None


def test_unknown_timezone_falls_back_to_utc_and_warns(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="soma.ingest.wahoo"):
        row = map_workout(workout(summary={"time_zone": "Mars/Olympus_Mons"}))
    assert row is not None
    assert row.started_at == dt.datetime(2026, 8, 24, 16, 26, 15)  # noqa: DTZ001 - UTC, unconverted
    assert any("time_zone" in r.getMessage() for r in caplog.records)


def test_source_and_external_id_scope_the_row():
    # Uniqueness is (source, external_id), so two vendors never collide.
    row = map_workout(workout())
    assert row is not None
    assert row.source == SOURCE == "wahoo"
    assert row.external_id == "490002584"


def test_raw_is_retained_whole():
    # Ingested rows keep the vendor response so a mapping fix is a remap rather
    # than a resync — and the workout_token identifies SYSTM-origin rides.
    row = map_workout(workout())
    assert row is not None
    assert row.raw["workout_token"] == "SYSTM fqf3lvHDvT:0"


@pytest.mark.parametrize(
    ("type_id", "expected"),
    [(12, "cycling"), (66, "yoga"), (999, "wahoo_type_999"), (None, None)],
)
def test_sport_mapping_falls_back_rather_than_guessing(type_id, expected):
    assert _sport(type_id) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("52.3", 52.3),
        (52.3, 52.3),
        ("0", 0.0),
        (None, None),
        ("", None),
        ("n/a", None),
        (True, None),
    ],
)
def test_num_coercion(value, expected):
    assert _num(value) == expected
