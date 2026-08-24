"""A day the phone recorded is not a day the watch recorded.

Garmin Connect logs steps from the phone even when the watch has not synced, so
such a day returns a payload that is not empty — it just holds nothing the
recovery rules can use. `_day_exists` treated any stored row as "done", so
`--skip-existing` (which is what compose runs nightly) skipped those days
forever, and watch data arriving late was never picked up.

That is the permanent hole the sync was written to avoid: its own docstring
promises "a day can gain data later — a watch synced late".
"""

from __future__ import annotations

from datetime import date, timedelta

from soma.db import get_session
from soma.ingest.garmin.sync import _day_is_synced, _should_fetch
from soma.models import DailyHealth

TODAY = date(2026, 8, 24)


def _phone_only(d: date) -> DailyHealth:
    # Steps come from the phone. Everything else needs the watch.
    return DailyHealth(date=d, steps=4000)


def _watch(d: date) -> DailyHealth:
    return DailyHealth(date=d, steps=4000, resting_hr=51, sleep_score=80, hrv_ms=55)


def test_a_phone_only_day_is_not_synced(db):
    with get_session() as s:
        s.merge(_phone_only(TODAY - timedelta(days=10)))
        s.commit()
    assert _day_is_synced(TODAY - timedelta(days=10)) is False


def test_a_watch_day_is_synced(db):
    with get_session() as s:
        s.merge(_watch(TODAY - timedelta(days=10)))
        s.commit()
    assert _day_is_synced(TODAY - timedelta(days=10)) is True


def test_a_day_with_only_steps_is_refetched_despite_skip_existing(db):
    # The reported bug: this day would be skipped forever, so sleep and HRV
    # arriving later are lost.
    d = TODAY - timedelta(days=10)
    with get_session() as s:
        s.merge(_phone_only(d))
        s.commit()
    assert _should_fetch(d, TODAY, skip_existing=True, refresh_days=2, recheck_days=30) is True


def test_a_complete_day_is_skipped(db):
    d = TODAY - timedelta(days=10)
    with get_session() as s:
        s.merge(_watch(d))
        s.commit()
    assert _should_fetch(d, TODAY, skip_existing=True, refresh_days=2, recheck_days=30) is False


def test_recent_days_are_always_refetched(db):
    # refresh_days exists so the last couple of days are re-read as Garmin
    # finishes processing them.
    d = TODAY - timedelta(days=1)
    with get_session() as s:
        s.merge(_watch(d))
        s.commit()
    assert _should_fetch(d, TODAY, skip_existing=True, refresh_days=2, recheck_days=30) is True


def test_hoping_for_late_data_is_bounded(db):
    # A day the watch never recorded should not be retried forever: past the
    # recheck window it is accepted as permanently phone-only, or a year
    # backfill re-fetches the same dead days every night.
    d = TODAY - timedelta(days=200)
    with get_session() as s:
        s.merge(_phone_only(d))
        s.commit()
    assert _should_fetch(d, TODAY, skip_existing=True, refresh_days=2, recheck_days=30) is False


def test_a_missing_day_is_always_fetched(db):
    d = TODAY - timedelta(days=10)
    assert _should_fetch(d, TODAY, skip_existing=True, refresh_days=2, recheck_days=30) is True


def test_without_skip_existing_everything_is_fetched(db):
    d = TODAY - timedelta(days=200)
    with get_session() as s:
        s.merge(_watch(d))
        s.commit()
    assert _should_fetch(d, TODAY, skip_existing=False, refresh_days=2, recheck_days=30) is True


def test_coverage_does_not_count_a_phone_only_day_as_present(db):
    """The report said 14 of 14 days present while three held no recovery data.

    Coverage exists so a gap cannot be mistaken for a rest day. Counting a row
    that holds only a step count defeats that at the one place it matters.
    """
    from soma.serve.queries import get_health_trend

    with get_session() as s:
        s.merge(_watch(TODAY))
        s.merge(_phone_only(TODAY - timedelta(days=1)))
        s.merge(_watch(TODAY - timedelta(days=2)))
        s.commit()

    trend = get_health_trend(days=3)
    cov = trend["coverage"]
    assert cov["days_present"] == 2, "a steps-only day is not a day the watch reported"
    assert cov["days_missing"] == 1


def test_coverage_reports_presence_per_signal(db):
    """A day can have a resting HR and no sleep. "Present" is not one thing."""
    from soma.serve.queries import get_health_trend

    with get_session() as s:
        s.merge(DailyHealth(date=TODAY, resting_hr=51))  # no sleep, no hrv
        s.merge(_watch(TODAY - timedelta(days=1)))
        s.commit()

    by_signal = get_health_trend(days=2)["coverage"]["by_signal"]
    assert by_signal["resting_hr"] == 2
    assert by_signal["sleep_score"] == 1, "one of the two days has no sleep"
    assert by_signal["hrv_ms"] == 1
