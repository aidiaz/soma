"""HTTP entry point: the MCP server behind Google OAuth.

Deployment shape is a Raspberry Pi behind a Cloudflare Tunnel. The tunnel
terminates TLS at the edge and opens no inbound port on the Pi.

**Do not put Cloudflare Access in front of this app.** The MCP OAuth handshake
needs ``/.well-known/oauth-protected-resource/mcp`` and
``/.well-known/oauth-authorization-server`` to be fetchable *without* a login.
Access answers them with an HTML login page, and the client gives up before the
OAuth flow can start. The email allowlist in :mod:`traindb.serve.oauth`
is what keeps other people out.
"""

from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from traindb.serve.oauth import build_auth
from traindb.config import settings
from traindb.db import init_db
from traindb.serve.server import SCOPE, build_mcp

log = logging.getLogger("traindb.serve.app")


class InsecureConfigError(RuntimeError):
    """Raised when the HTTP server would start with no way to reject a stranger."""


def build_http_auth():
    """Pick the auth provider for the HTTP transport.

    Google OAuth when it is configured; otherwise the static bearer token, which
    is intended for local development. If neither is set the server refuses to
    start — an open ``/mcp`` on a public tunnel serves a year of health data to
    anyone who finds the URL.
    """
    auth = build_auth(settings)  # raises AllowlistEmptyError if OAuth is on but unguarded
    if auth is not None:
        log.info("Auth: Google OAuth, %d allowed email(s).", len(settings.allowed_emails))
        return auth
    if settings.api_token:
        log.warning("Auth: static bearer token. Intended for local development only.")
        return StaticTokenVerifier(
            tokens={settings.api_token: {"client_id": "traindb", "scopes": [SCOPE]}},
            required_scopes=[SCOPE],
        )
    raise InsecureConfigError(
        "No authentication configured. Set TRAINDB_GOOGLE_CLIENT_ID / "
        "TRAINDB_GOOGLE_CLIENT_SECRET / TRAINDB_BASE_URL / TRAINDB_ALLOWED_EMAILS for "
        "OAuth, or TRAINDB_API_TOKEN for a local bearer token."
    )


def create_app() -> FastAPI:
    """Build the FastAPI app hosting the MCP transport at ``/mcp``."""
    init_db()
    mcp = build_mcp(auth=build_http_auth())
    # Stateless: no per-client session to lose when the container restarts or the
    # tunnel reconnects.
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

    app = FastAPI(title="traindb", lifespan=mcp_app.lifespan)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # Mounted at the root, not at "/mcp": OAuth discovery (/.well-known/*) and
    # dynamic client registration (/register) must sit at the domain root, which
    # is where MCP clients probe. /health is registered first, so it still wins.
    app.mount("/", mcp_app)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Garmin MCP on http://%s:%s/mcp  (health: /health)", settings.host, settings.port)
    # factory=True keeps app construction out of import time, so importing this
    # module for a test never trips the auth checks in create_app().
    uvicorn.run(create_app, factory=True, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
