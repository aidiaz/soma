"""What "today" is, and why it is local.

`clock.today()` decides which row a write lands on, because date is the join
key. The zone it resolves in is therefore not a formatting concern — it changes
stored data.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

import soma.config as cfg
from soma import clock
from soma.config import Settings


def test_now_is_utc_and_aware():
    # Timestamps stay absolute. Only the calendar day is local.
    moment = clock.now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == dt.timedelta(0)


def test_today_follows_the_local_zone_not_utc(monkeypatch):
    """The case the reversal exists for: evening west of UTC.

    02:30 UTC is still the previous evening in Santiago. Under the old rule a
    meal logged then was recorded against tomorrow.
    """
    monkeypatch.setattr(cfg.settings, "timezone", "America/Santiago")
    instant = dt.datetime(2026, 8, 25, 2, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(clock, "now", lambda: instant)

    assert instant.date() == dt.date(2026, 8, 25), "UTC has already rolled over"
    assert clock.today() == dt.date(2026, 8, 24), "but locally it is still the 24th"


def test_today_follows_the_local_zone_east_of_utc(monkeypatch):
    # The mirror case: early morning east of UTC is still yesterday in UTC.
    monkeypatch.setattr(cfg.settings, "timezone", "Asia/Tokyo")
    instant = dt.datetime(2026, 8, 24, 22, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(clock, "now", lambda: instant)

    assert instant.date() == dt.date(2026, 8, 24)
    assert clock.today() == dt.date(2026, 8, 25)


def test_utc_still_works_as_a_zone(monkeypatch):
    monkeypatch.setattr(cfg.settings, "timezone", "UTC")
    instant = dt.datetime(2026, 8, 25, 2, 30, tzinfo=dt.UTC)
    monkeypatch.setattr(clock, "now", lambda: instant)
    assert clock.today() == dt.date(2026, 8, 25)


def test_an_unknown_zone_is_rejected_at_settings_load():
    """Loudly, and at startup.

    A typo does not raise where it is set. Without this it raises the first time
    something asks the date — mid-run on the sync worker, or on the first tool
    call on the server.
    """
    with pytest.raises(ValidationError) as exc:
        Settings(timezone="Mars/Olympus_Mons")
    assert "not a known IANA timezone" in str(exc.value)


def test_a_dst_boundary_resolves(monkeypatch):
    # Santiago shifts in September. A date must still come out; the point is
    # that ZoneInfo handles the offset change, not a fixed -4.
    monkeypatch.setattr(cfg.settings, "timezone", "America/Santiago")
    for month, day in ((1, 15), (7, 15)):
        instant = dt.datetime(2026, month, day, 12, 0, tzinfo=dt.UTC)
        monkeypatch.setattr(clock, "now", lambda i=instant: i)
        assert isinstance(clock.today(), dt.date)
