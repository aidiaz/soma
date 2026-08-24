"""Shared fixtures.

Every test that touches the database runs against a fresh file in ``tmp_path``.
``db.get_engine`` builds lazily and caches, so a test must repoint
``settings.db_path`` *and* call ``reset_engine()`` — the ``db`` fixture does
both, on the way in and on the way out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from traindb.config import Settings, settings
from traindb.db import get_session, init_db, reset_engine
from traindb.models import Activity, Body, DailyHealth, Nutrition


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty database, isolated per test."""
    monkeypatch.setattr(settings, "db_path", tmp_path / "traindb.db")
    reset_engine()
    init_db()
    yield
    reset_engine()


@pytest.fixture
def tokenstore(tmp_path, monkeypatch):
    """An isolated Garmin token store, so cooldown tests never touch the real one."""
    path = tmp_path / "tokens"
    monkeypatch.setattr(settings, "garmin_tokenstore", path)
    return path


def make_settings(**overrides) -> Settings:
    """A Settings built from explicit values only.

    ``_env_file=None`` matters: without it pydantic-settings reads the
    developer's real ``.env``, and a test asserting "OAuth is not configured"
    would pass or fail depending on whose machine it ran on.
    """
    base = {
        "oauth_enabled": True,
        "google_client_id": "test-client-id.apps.googleusercontent.com",
        "google_client_secret": "test-secret",
        "base_url": "https://traindb.example.test",
        "allowed_emails": {"owner@example.com"},
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def seeded(db):
    """A full week of correlated data, plus the prior week to compare against.

    Anchored on *last* week's Monday: aligned to an ISO week so grouping is
    stable, and wholly in the past so "the last N days" windows behave the same
    whichever weekday the suite runs on.

    Deliberately incomplete — three days of health, two of food — because the
    coverage reporting is only meaningful when some days are genuinely missing.
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday() + 7)
    days = [monday, monday + timedelta(days=1), monday + timedelta(days=2)]
    prior_monday = monday - timedelta(days=7)

    with get_session() as session:
        for i, day in enumerate(days):
            session.merge(
                DailyHealth(
                    date=day,
                    sleep_score=80 + i,
                    sleep_duration_s=25_200 + i * 1_800,
                    hrv_status="BALANCED",
                    hrv_ms=60 + i,
                    resting_hr=50 + i,
                    body_battery_high=90,
                    body_battery_low=20,
                    steps=10_000 + i * 1_000,
                    raw={"stats": {"totalSteps": 10_000 + i * 1_000}},
                )
            )
        # One prior-week day, so the week-over-week deltas have something to
        # compare against instead of reporting None.
        session.merge(
            DailyHealth(date=prior_monday, resting_hr=48, hrv_ms=65, sleep_score=85, raw={})
        )

        session.merge(
            Activity(
                id=1,
                source="garmin",
                external_id="1001",
                started_at=datetime.combine(days[0], datetime.min.time()).replace(hour=7),
                date=days[0],
                sport="cycling",
                duration_s=3_000,
                tss=120.0,
                avg_power=200.0,
                avg_hr=140.0,
                work_kj=600.0,
                raw={"activityId": 1001},
            )
        )
        session.merge(
            Activity(
                id=2,
                source="garmin",
                external_id="1002",
                started_at=datetime.combine(days[2], datetime.min.time()).replace(hour=18),
                date=days[2],
                sport="cycling",
                duration_s=3_600,
                tss=80.0,
                avg_power=180.0,
                work_kj=648.0,
                raw={"activityId": 1002},
            )
        )
        # The prior week carries load too, so the ramp is a real ratio rather
        # than a division by zero.
        session.merge(
            Activity(
                id=3,
                source="garmin",
                external_id="1000",
                started_at=datetime.combine(prior_monday, datetime.min.time()).replace(hour=7),
                date=prior_monday,
                sport="cycling",
                duration_s=2_400,
                tss=100.0,
                raw={},
            )
        )

        session.merge(Nutrition(date=days[0], kcal=2_400, protein_g=150.0, creatine=True))
        session.merge(Nutrition(date=days[1], kcal=2_200, protein_g=140.0))

        session.merge(Body(date=days[0], weight_kg=72.0, waist_cm=84.0))
        session.merge(Body(date=prior_monday, weight_kg=72.8, waist_cm=85.0))
        session.commit()

    return {"days": days, "monday": monday, "prior_monday": prior_monday}
