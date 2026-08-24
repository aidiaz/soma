"""Settings parsing, with attention to the allowlist.

The allowlist arrives from the environment as one comma-separated string. If it
parsed to something falsy by accident, :func:`build_auth` would raise rather than
admit everyone — but a *wrongly populated* list silently admits the wrong person,
so the normalisation is worth pinning down.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_settings


def test_allowed_emails_splits_on_commas():
    s = make_settings(allowed_emails="a@x.com,b@y.com")
    assert s.allowed_emails == {"a@x.com", "b@y.com"}


def test_allowed_emails_strips_whitespace():
    s = make_settings(allowed_emails=" a@x.com ,  b@y.com ")
    assert s.allowed_emails == {"a@x.com", "b@y.com"}


def test_allowed_emails_lowercases():
    # Google returns the email as the user typed it at sign-up; comparison must
    # not depend on that casing.
    s = make_settings(allowed_emails="Owner@Example.COM")
    assert s.allowed_emails == {"owner@example.com"}


def test_allowed_emails_drops_empty_entries():
    s = make_settings(allowed_emails="a@x.com,,  ,b@y.com,")
    assert s.allowed_emails == {"a@x.com", "b@y.com"}


def test_allowed_emails_empty_string_is_empty_set():
    assert make_settings(allowed_emails="").allowed_emails == set()


def test_allowed_emails_accepts_a_collection():
    s = make_settings(allowed_emails=["A@x.com", "b@Y.com"])
    assert s.allowed_emails == {"a@x.com", "b@y.com"}


def test_oauth_configured_when_complete():
    assert make_settings().oauth_configured is True


def test_oauth_not_configured_when_disabled():
    assert make_settings(oauth_enabled=False).oauth_configured is False


def test_oauth_not_configured_without_client_id():
    assert make_settings(google_client_id="").oauth_configured is False


def test_oauth_not_configured_without_secret():
    assert make_settings(google_client_secret="").oauth_configured is False


def test_oauth_not_configured_without_base_url():
    assert make_settings(base_url="").oauth_configured is False


def test_db_url_is_sqlite():
    s = make_settings(db_path=Path("/tmp/x.db"))
    assert s.db_url == "sqlite:////tmp/x.db"


def test_cooldown_file_lives_in_the_token_store():
    s = make_settings(garmin_tokenstore=Path("/tmp/tokens"))
    assert s.garmin_cooldown_file == Path("/tmp/tokens/last_login_attempt")
