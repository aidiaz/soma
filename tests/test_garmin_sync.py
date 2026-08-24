"""Garmin ingestion: the mappers, the empty-day guard, and the raw-payload contract."""

from __future__ import annotations

from datetime import date, datetime

from sqlmodel import select

from traindb.db import get_session
from traindb.ingest.garmin.remap import remap
from traindb.ingest.garmin.sync import (
    _is_placeholder,
    _parse_date,
    _parse_dt,
    _pick,
    _write,
    map_activity,
    map_daily_health,
    sync_day,
)
from traindb.models import Activity, DailyHealth

DAY = date(2026, 3, 14)


# --- helpers ---------------------------------------------------------------


def test_pick_returns_the_first_present_key():
    assert _pick({"a": 1, "b": 2}, "a", "b") == 1


def test_pick_skips_none_values():
    assert _pick({"a": None, "b": 2}, "a", "b") == 2


def test_pick_falls_back_to_the_default():
    assert _pick({}, "a", default="x") == "x"


def test_pick_tolerates_a_non_dict():
    assert _pick(None, "a", default="x") == "x"


def test_parse_dt_reads_garmin_epoch_millis():
    assert _parse_dt(1_773_000_000_000).year == 2026


def test_parse_dt_reads_a_zulu_string():
    assert _parse_dt("2026-03-14T07:30:00Z") == datetime(2026, 3, 14, 7, 30)


def test_parse_dt_reads_fractional_seconds():
    assert _parse_dt("2026-03-14 07:30:00.500") == datetime(2026, 3, 14, 7, 30, 0, 500_000)


def test_parse_dt_returns_none_for_junk():
    assert _parse_dt("not a date") is None


def test_parse_dt_survives_an_absurd_epoch():
    assert _parse_dt(10**20) is None


def test_parse_date_truncates_a_datetime_string():
    assert _parse_date("2026-03-14T07:30:00") == DAY


# --- map_activity ----------------------------------------------------------


def test_activity_without_an_id_is_dropped():
    assert map_activity({"startTimeLocal": "2026-03-14 07:00:00"}) is None


def test_activity_without_a_start_time_is_dropped():
    # date is the join key to every other table, so a row that cannot supply
    # one has nothing to join on.
    assert map_activity({"activityId": 1}) is None


def test_activity_date_comes_from_local_time(db):
    # A 23:30 ride belongs to that day's training, not the next one's.
    row = map_activity({"activityId": 1, "startTimeLocal": "2026-03-14 23:30:00"})
    assert row.date == DAY


def test_activity_prefers_local_over_gmt():
    row = map_activity(
        {
            "activityId": 1,
            "startTimeLocal": "2026-03-14 07:00:00",
            "startTimeGMT": "2026-03-14 10:00:00",
        }
    )
    assert row.started_at.hour == 7


def test_activity_carries_the_source_and_external_id():
    row = map_activity({"activityId": 4242, "startTimeLocal": "2026-03-14 07:00:00"})
    assert (row.source, row.external_id) == ("garmin", "4242")


def test_activity_reads_the_nested_type_key():
    row = map_activity(
        {
            "activityId": 1,
            "startTimeLocal": "2026-03-14 07:00:00",
            "activityType": {"typeKey": "cycling"},
        }
    )
    assert row.sport == "cycling"


def test_garmin_training_load_lands_in_tss():
    row = map_activity(
        {"activityId": 1, "startTimeLocal": "2026-03-14 07:00:00", "activityTrainingLoad": 118.0}
    )
    assert row.tss == 118.0


def test_work_kj_is_derived_from_power_and_duration():
    row = map_activity(
        {
            "activityId": 1,
            "startTimeLocal": "2026-03-14 07:00:00",
            "avgPower": 200,
            "duration": 3_600,
        }
    )
    assert row.work_kj == 720.0


def test_work_kj_is_none_without_power():
    row = map_activity(
        {"activityId": 1, "startTimeLocal": "2026-03-14 07:00:00", "duration": 3_600}
    )
    assert row.work_kj is None


def test_activity_keeps_the_whole_payload():
    row = map_activity(
        {"activityId": 1, "startTimeLocal": "2026-03-14 07:00:00", "someFutureField": "kept"}
    )
    assert row.raw["someFutureField"] == "kept"


# --- map_daily_health ------------------------------------------------------


def test_daily_health_collapses_five_payloads_into_one_row():
    row = map_daily_health(
        DAY,
        stats={"totalSteps": 9_000, "restingHeartRate": 48, "bodyBatteryHighestValue": 95},
        sleep={
            "dailySleepDTO": {"sleepTimeSeconds": 27_000, "sleepScores": {"overall": {"value": 84}}}
        },
        hrv={"hrvSummary": {"lastNightAvg": 61, "status": "BALANCED"}},
    )
    assert (row.steps, row.resting_hr, row.body_battery_high) == (9_000, 48, 95)
    assert (row.sleep_duration_s, row.sleep_score) == (27_000, 84)
    assert (row.hrv_ms, row.hrv_status) == (61, "BALANCED")


def test_daily_health_falls_back_to_the_flat_sleep_score():
    row = map_daily_health(DAY, sleep={"dailySleepDTO": {"sleepScoreValue": 79}})
    assert row.sleep_score == 79


def test_daily_health_tolerates_every_payload_missing():
    row = map_daily_health(DAY)
    assert row.date == DAY
    assert row.steps is None


def test_vo2max_is_kept_in_raw_even_though_it_has_no_column():
    # Dropping it would be irreversible: Garmin ages data out, so a later
    # decision to want it back could not be honoured for history.
    row = map_daily_health(DAY, max_metrics=[{"generic": {"vo2MaxValue": 53.0}}])
    assert row.raw["max_metrics"][0]["generic"]["vo2MaxValue"] == 53.0


def test_training_status_is_kept_in_raw(db):
    row = map_daily_health(DAY, training_status={"mostRecentTrainingStatus": {"x": 1}})
    assert row.raw["training_status"]["mostRecentTrainingStatus"] == {"x": 1}


# --- the empty-day guard ---------------------------------------------------


def test_a_date_only_health_row_is_a_placeholder():
    assert _is_placeholder(DailyHealth(date=DAY)) is True


def test_a_row_with_one_measurement_is_not():
    assert _is_placeholder(DailyHealth(date=DAY, steps=1)) is False


def test_a_zero_measurement_is_still_data():
    # Zero steps on a recorded day is a fact; None is the absence of one.
    assert _is_placeholder(DailyHealth(date=DAY, steps=0)) is False


def test_raw_alone_does_not_count_as_data():
    # Garmin's empty payload still echoes the date back, so raw is populated for
    # exactly the rows this guard exists to reject.
    assert (
        _is_placeholder(DailyHealth(date=DAY, raw={"stats": {"calendarDate": "2026-03-14"}}))
        is True
    )


def test_an_empty_garmin_day_maps_to_a_placeholder():
    assert _is_placeholder(map_daily_health(DAY, stats={"calendarDate": "2026-03-14"})) is True


def test_write_rejects_none(db):
    with get_session() as session:
        assert _write(session, None) is False


def test_write_rejects_a_placeholder(db):
    with get_session() as session:
        assert _write(session, DailyHealth(date=DAY)) is False
        session.commit()
    with get_session() as session:
        assert session.get(DailyHealth, DAY) is None


def test_write_persists_a_real_row(db):
    with get_session() as session:
        assert _write(session, DailyHealth(date=DAY, steps=5_000)) is True
        session.commit()
    with get_session() as session:
        assert session.get(DailyHealth, DAY).steps == 5_000


class FakeClient:
    """A Garmin client returning empty-but-present payloads for every endpoint."""

    def __init__(self, stats=None, sleep=None, hrv=None):
        self._stats = stats if stats is not None else {"calendarDate": DAY.isoformat()}
        self._sleep = sleep if sleep is not None else {}
        self._hrv = hrv if hrv is not None else {}

    def get_stats(self, iso):
        return self._stats

    def get_sleep_data(self, iso):
        return self._sleep

    def get_hrv_data(self, iso):
        return self._hrv

    def get_training_status(self, iso):
        return {}

    def get_max_metrics(self, iso):
        return []


def test_sync_day_writes_nothing_for_a_day_with_no_watch_data(db):
    assert sync_day(FakeClient(), DAY) == 0
    with get_session() as session:
        assert session.get(DailyHealth, DAY) is None


def test_sync_day_writes_a_day_that_has_data(db):
    client = FakeClient(stats={"calendarDate": DAY.isoformat(), "totalSteps": 9_100})
    assert sync_day(client, DAY) == 1
    with get_session() as session:
        assert session.get(DailyHealth, DAY).steps == 9_100


def test_a_failing_endpoint_does_not_stop_the_day(db):
    class Broken(FakeClient):
        def get_hrv_data(self, iso):
            raise RuntimeError("Garmin changed something")

    client = Broken(stats={"totalSteps": 9_100})
    assert sync_day(client, DAY) == 1
    with get_session() as session:
        assert session.get(DailyHealth, DAY).hrv_ms is None


# --- remap -----------------------------------------------------------------


def test_remap_recovers_a_field_that_was_not_extracted(db):
    # The row was stored before the mapper knew about restingHeartRate, but raw
    # kept it all along.
    with get_session() as session:
        session.merge(
            DailyHealth(
                date=DAY,
                steps=9_000,
                resting_hr=None,
                raw={"stats": {"totalSteps": 9_000, "restingHeartRate": 48}},
            )
        )
        session.commit()

    remap()

    with get_session() as session:
        assert session.get(DailyHealth, DAY).resting_hr == 48


def test_remap_is_idempotent(db):
    with get_session() as session:
        session.merge(DailyHealth(date=DAY, raw={"stats": {"totalSteps": 9_000}}))
        session.commit()
    remap()
    remap()
    with get_session() as session:
        assert session.get(DailyHealth, DAY).steps == 9_000


def test_remap_leaves_a_row_alone_when_raw_yields_nothing(db):
    # A mapping regression must not flatten a good row into a placeholder.
    with get_session() as session:
        session.merge(DailyHealth(date=DAY, steps=9_000, raw={}))
        session.commit()
    remap()
    with get_session() as session:
        assert session.get(DailyHealth, DAY).steps == 9_000


def test_remap_keeps_the_surrogate_key_on_activities(db):
    with get_session() as session:
        session.merge(
            Activity(
                id=7,
                source="garmin",
                external_id="1001",
                date=DAY,
                raw={"activityId": 1001, "startTimeLocal": "2026-03-14 07:00:00", "avgPower": 210},
            )
        )
        session.commit()

    remap()

    with get_session() as session:
        rows = session.exec(select(Activity)).all()
    assert len(rows) == 1
    assert rows[0].id == 7
    assert rows[0].avg_power == 210


def test_remap_on_an_empty_database_does_nothing(db):
    remap()
