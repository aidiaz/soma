"""The email allowlist — the only thing that makes Google OAuth an *authorisation* gate.

FastMCP's GoogleProvider will happily complete the flow for any Google account
in the world. Everything here is about what happens after that succeeds.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import make_settings
from fastmcp.server.auth.providers.google import GoogleProvider

from traindb.serve.oauth import (
    EMAIL_SCOPE,
    AllowlistEmptyError,
    build_auth,
    is_allowed,
)


class FakeAccessToken:
    def __init__(self, claims):
        self.claims = claims


def upstream_returns(value):
    """Patch the parent provider so verify_token exercises only our gate."""

    async def fake_verify(self, token):
        return value

    return patch.object(GoogleProvider, "verify_token", fake_verify)


# --- is_allowed ------------------------------------------------------------


def test_is_allowed_exact_match():
    assert is_allowed(make_settings(), "owner@example.com") is True


def test_is_allowed_is_case_insensitive():
    assert is_allowed(make_settings(), "Owner@Example.com") is True


def test_is_allowed_ignores_surrounding_whitespace():
    assert is_allowed(make_settings(), "  owner@example.com  ") is True


def test_is_allowed_rejects_a_stranger():
    assert is_allowed(make_settings(), "someone@else.com") is False


def test_is_allowed_rejects_none():
    assert is_allowed(make_settings(), None) is False


def test_is_allowed_rejects_empty_string():
    assert is_allowed(make_settings(), "") is False


def test_is_allowed_rejects_everyone_when_the_list_is_empty():
    assert is_allowed(make_settings(allowed_emails=""), "owner@example.com") is False


# --- build_auth ------------------------------------------------------------


def test_build_auth_returns_none_when_oauth_disabled():
    assert build_auth(make_settings(oauth_enabled=False)) is None


def test_build_auth_returns_none_without_credentials():
    assert build_auth(make_settings(google_client_id="")) is None


def test_build_auth_returns_none_without_a_base_url():
    assert build_auth(make_settings(base_url="")) is None


def test_build_auth_refuses_an_empty_allowlist():
    # Refusing to start is the point: an unguarded OAuth server authenticates
    # every Google user and authorises all of them.
    with pytest.raises(AllowlistEmptyError):
        build_auth(make_settings(allowed_emails=""))


def test_build_auth_error_names_the_variable_to_set():
    with pytest.raises(AllowlistEmptyError, match="TRAINDB_ALLOWED_EMAILS"):
        build_auth(make_settings(allowed_emails=""))


def test_build_auth_returns_a_provider_when_configured():
    assert isinstance(build_auth(make_settings()), GoogleProvider)


def test_build_auth_requests_only_the_email_scope():
    # Google's access-token introspection does not reliably report "openid" as
    # granted, so requiring it fails verification after a successful sign-in.
    provider = build_auth(make_settings())
    assert list(provider.required_scopes) == [EMAIL_SCOPE]


def test_build_auth_strips_a_trailing_slash_from_base_url():
    provider = build_auth(make_settings(base_url="https://traindb.example.test/"))
    assert not str(provider.base_url).endswith("//")


# --- verify_token ----------------------------------------------------------


async def test_verify_token_admits_an_allowlisted_email():
    provider = build_auth(make_settings())
    token = FakeAccessToken({"email": "owner@example.com"})
    with upstream_returns(token):
        assert await provider.verify_token("jwt") is token


async def test_verify_token_admits_regardless_of_claim_casing():
    provider = build_auth(make_settings())
    token = FakeAccessToken({"email": "Owner@Example.COM"})
    with upstream_returns(token):
        assert await provider.verify_token("jwt") is token


async def test_verify_token_rejects_a_stranger():
    provider = build_auth(make_settings())
    with upstream_returns(FakeAccessToken({"email": "stranger@elsewhere.com"})):
        assert await provider.verify_token("jwt") is None


async def test_verify_token_rejects_when_upstream_fails():
    provider = build_auth(make_settings())
    with upstream_returns(None):
        assert await provider.verify_token("jwt") is None


async def test_verify_token_rejects_a_token_with_no_email_claim():
    provider = build_auth(make_settings())
    with upstream_returns(FakeAccessToken({"sub": "12345"})):
        assert await provider.verify_token("jwt") is None


async def test_verify_token_rejects_a_token_with_no_claims():
    provider = build_auth(make_settings())
    with upstream_returns(FakeAccessToken(None)):
        assert await provider.verify_token("jwt") is None
