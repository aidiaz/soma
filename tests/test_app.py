"""The HTTP entry point, and its refusal to start without a gate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from traindb.serve.app import InsecureConfigError, build_http_auth, create_app
from traindb.serve.oauth import AllowlistEmptyError
from traindb.config import settings


@pytest.fixture
def unconfigured(monkeypatch):
    """Strip every credential, so each test opts back in to what it needs."""
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "oauth_enabled", False)
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(settings, "base_url", "")
    monkeypatch.setattr(settings, "allowed_emails", set())


def _enable_oauth(monkeypatch, emails):
    monkeypatch.setattr(settings, "oauth_enabled", True)
    monkeypatch.setattr(settings, "google_client_id", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "secret")
    monkeypatch.setattr(settings, "base_url", "https://garmin.example.test")
    monkeypatch.setattr(settings, "allowed_emails", emails)


def test_refuses_to_start_with_no_authentication(unconfigured):
    # A public tunnel plus an open /mcp serves a year of health data to anyone
    # who finds the URL. Failing loudly at startup is the whole point.
    with pytest.raises(InsecureConfigError):
        build_http_auth()


def test_the_refusal_names_the_variables_to_set(unconfigured):
    with pytest.raises(InsecureConfigError, match="TRAINDB_API_TOKEN"):
        build_http_auth()


def test_falls_back_to_the_static_token(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    assert isinstance(build_http_auth(), StaticTokenVerifier)


def test_oauth_wins_over_the_static_token(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    _enable_oauth(monkeypatch, {"owner@example.com"})
    assert isinstance(build_http_auth(), GoogleProvider)


def test_an_empty_allowlist_stops_startup_even_with_a_token(unconfigured, monkeypatch):
    # The token must not paper over a misconfigured allowlist: OAuth is on, so
    # the operator believes the allowlist is what is guarding the server.
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    _enable_oauth(monkeypatch, set())
    with pytest.raises(AllowlistEmptyError):
        build_http_auth()


def test_create_app_serves_health(db, unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_reachable_without_a_token(db, unconfigured, monkeypatch):
    # The container healthcheck has no credentials.
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_mcp_rejects_an_unauthenticated_call(db, unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    with TestClient(create_app()) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401


def test_mcp_rejects_a_wrong_token(db, unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "local-dev-token")
    with TestClient(create_app()) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer not-the-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert response.status_code == 401


def test_oauth_discovery_is_reachable_without_a_login(db, unconfigured, monkeypatch):
    # These two paths must answer before any credential exists, which is why no
    # Cloudflare Access policy may sit in front of this app.
    _enable_oauth(monkeypatch, {"owner@example.com"})
    with TestClient(create_app()) as client:
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


def test_dynamic_client_registration_is_published(db, unconfigured, monkeypatch):
    # Claude Code needs no --client-id because the server advertises /register.
    _enable_oauth(monkeypatch, {"owner@example.com"})
    with TestClient(create_app()) as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" in metadata


def test_the_google_redirect_uri_is_the_documented_one(db, unconfigured, monkeypatch):
    # This is the URI that has to be registered in the Google console.
    _enable_oauth(monkeypatch, {"owner@example.com"})
    app = create_app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    mounted = {
        route.path for mount in app.routes if hasattr(mount, "routes") for route in mount.routes
    }
    assert "/auth/callback" in paths | mounted
