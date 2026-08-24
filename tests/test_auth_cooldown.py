"""The persisted cooldown on credential logins.

Garmin's SSO limit is per account, so a retry loop does not just fail — it
extends the lockout. An in-memory delay would not survive `garmin-auth` being
run again in a new shell, which is exactly how a person retries.
"""

from __future__ import annotations

import time

from soma.config import settings
from soma.ingest.garmin.auth import (
    cooldown_remaining,
    last_attempt_at,
    record_attempt,
)


def test_no_marker_means_no_cooldown(tokenstore):
    assert last_attempt_at() is None
    assert cooldown_remaining() == 0.0


def test_record_attempt_creates_the_token_store(tokenstore):
    assert not tokenstore.exists()
    record_attempt()
    assert settings.garmin_cooldown_file.exists()


def test_record_attempt_stamps_a_readable_time(tokenstore):
    now = time.time()
    record_attempt(now=now)
    assert last_attempt_at() == now


def test_a_fresh_attempt_blocks_the_next_one(tokenstore):
    record_attempt()
    assert cooldown_remaining() > 0


def test_cooldown_expires(tokenstore, monkeypatch):
    monkeypatch.setattr(settings, "garmin_login_cooldown_s", 900.0)
    stamped = time.time()
    record_attempt(now=stamped)
    assert cooldown_remaining(now=stamped + 901) == 0.0


def test_cooldown_counts_down(tokenstore, monkeypatch):
    monkeypatch.setattr(settings, "garmin_login_cooldown_s", 900.0)
    stamped = time.time()
    record_attempt(now=stamped)
    assert cooldown_remaining(now=stamped + 300) == 600.0


def test_unreadable_marker_is_treated_as_a_recent_attempt(tokenstore):
    # Failing open here would defeat the whole point: the unknown case must not
    # be the one that lets a retry through.
    record_attempt()
    settings.garmin_cooldown_file.write_text("not-a-timestamp")
    assert cooldown_remaining() > 0


def test_empty_marker_is_treated_as_a_recent_attempt(tokenstore):
    record_attempt()
    settings.garmin_cooldown_file.write_text("")
    assert cooldown_remaining() > 0
