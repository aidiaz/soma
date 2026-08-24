"""One effort stored twice must count once.

`models.Activity` is explicit that duplication is expected: uniqueness is on
`(source, external_id)`, so the same ride arriving from two vendors is two rows
describing one effort. `daily_tss` used to add them together, which inflates CTL
and reports a week harder than the one that happened — silently, which is the
dangerous kind of wrong for the number that drives coaching.

The matching is deliberately conservative, so these tests are as much about what
is *not* merged as what is.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from soma.db import get_session
from soma.metrics import daily_tss, duplicate_efforts, week_tss
from soma.models import Activity
from soma.serve.queries import get_training_week

MONDAY = date(2026, 1, 5)


def _add(session, *, source, external_id, day=MONDAY, hour=8, minute=0, tss=100.0, duration=3600.0):
    session.merge(
        Activity(
            source=source,
            external_id=external_id,
            started_at=datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute),
            date=day,
            tss=tss,
            duration_s=duration,
            raw={},
        )
    )


def test_one_effort_from_two_vendors_counts_once(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=95.0)
        _add(s, source="wahoo", external_id="w1", tss=100.0, minute=2)
        s.commit()
    # Not 195. The two rows are one ride.
    assert daily_tss(MONDAY, MONDAY)[MONDAY] == 100.0


def test_the_wahoo_copy_is_the_one_kept(db):
    # Wahoo reports true TSS and owns virtual rides, so it wins the match.
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=95.0)
        _add(s, source="wahoo", external_id="w1", tss=100.0, minute=2)
        s.commit()
    dropped = duplicate_efforts(MONDAY, MONDAY)
    assert len(dropped) == 1
    assert dropped[0]["kept"]["source"] == "wahoo"
    assert dropped[0]["dropped"]["source"] == "garmin"
    assert dropped[0]["tss_not_counted"] == 95.0


def test_two_different_rides_on_one_day_both_count(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", hour=7, tss=50.0)
        _add(s, source="wahoo", external_id="w1", hour=18, tss=70.0)
        s.commit()
    assert daily_tss(MONDAY, MONDAY)[MONDAY] == 120.0
    assert duplicate_efforts(MONDAY, MONDAY) == []


def test_close_in_time_but_different_length_both_count(db):
    # A 20-minute warm-up logged separately from a 2-hour session starts inside
    # the window but is not the same effort. Duration is what separates them.
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=15.0, duration=1200.0)
        _add(s, source="wahoo", external_id="w1", tss=110.0, duration=7200.0, minute=5)
        s.commit()
    assert daily_tss(MONDAY, MONDAY)[MONDAY] == 125.0


def test_same_source_is_never_deduped(db):
    # Two rows from one vendor are two efforts by definition — uniqueness is
    # already (source, external_id), so the vendor has said they are distinct.
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=40.0)
        _add(s, source="garmin", external_id="g2", tss=40.0, minute=1)
        s.commit()
    assert daily_tss(MONDAY, MONDAY)[MONDAY] == 80.0


def test_missing_duration_refuses_to_guess(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=40.0, duration=None)
        _add(s, source="wahoo", external_id="w1", tss=40.0, minute=1)
        s.commit()
    # Over-counting is visible in the coverage report; discarding real training
    # is not. When the evidence is incomplete, keep both.
    assert daily_tss(MONDAY, MONDAY)[MONDAY] == 80.0


def test_missing_start_time_refuses_to_guess(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=40.0)
        s.merge(
            Activity(
                source="wahoo",
                external_id="w1",
                started_at=None,
                date=MONDAY,
                tss=40.0,
                duration_s=3600.0,
                raw={},
            )
        )
        s.commit()
    assert daily_tss(MONDAY, MONDAY)[MONDAY] == 80.0


def test_a_duplicate_is_logged_as_a_warning(db, caplog):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1")
        _add(s, source="wahoo", external_id="w1", minute=2)
        s.commit()
    with caplog.at_level(logging.WARNING, logger="soma.metrics"):
        daily_tss(MONDAY, MONDAY)
    # Reaching here means the ingest-side filter let an overlap through.
    assert any("duplicate effort" in r.getMessage() for r in caplog.records)


def test_the_week_total_is_not_inflated(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=95.0)
        _add(s, source="wahoo", external_id="w1", tss=100.0, minute=2)
        _add(s, source="garmin", external_id="g2", day=MONDAY + timedelta(days=2), tss=60.0)
        s.commit()
    assert week_tss(MONDAY) == 160.0


def test_coverage_surfaces_the_duplicate(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=95.0)
        _add(s, source="wahoo", external_id="w1", tss=100.0, minute=2)
        s.commit()
    week = get_training_week(MONDAY.isoformat())
    dupes = week["coverage"]["duplicate_efforts"]
    assert len(dupes) == 1, "an overlap must be visible to the reader, not only in a log file"
    assert dupes[0]["dropped"]["external_id"] == "g1"


def test_coverage_is_empty_when_nothing_overlaps(db):
    with get_session() as s:
        _add(s, source="garmin", external_id="g1", tss=95.0)
        s.commit()
    assert get_training_week(MONDAY.isoformat())["coverage"]["duplicate_efforts"] == []
