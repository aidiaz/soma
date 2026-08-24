"""Email allowlist on top of Google OAuth.

FastMCP's ``GoogleProvider`` proves *who* a caller is. It has no notion of who
is *allowed* — any Google account on earth completes the flow successfully. This
module is the authorisation half: it wraps the provider so a verified token is
only accepted when its email claim is on ``TRAINDB_ALLOWED_EMAILS``.

Because that list is the only thing standing between the public internet and a
year of personal health data, :func:`build_auth` refuses to build a provider
when it is empty rather than defaulting open.
"""

from __future__ import annotations

import logging
from typing import Any

from traindb.config import Settings

log = logging.getLogger("traindb.serve.oauth")

# Google's access-token introspection does not reliably report "openid" as a
# granted scope, so requiring it here makes verification fail *after* an
# otherwise successful sign-in. Ask for the email scope and nothing else.
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"


class AllowlistEmptyError(RuntimeError):
    """Raised when OAuth is configured but no email is allowed to use it."""


def is_allowed(settings: Settings, email: str | None) -> bool:
    """Whether ``email`` may use the server. Comparison is case-insensitive."""
    if not email:
        return False
    return email.strip().lower() in settings.allowed_emails


def build_auth(settings: Settings) -> Any:
    """An allowlist-gated ``GoogleProvider``, or ``None`` if OAuth is off.

    Returning ``None`` lets local development and the stdio entry point fall
    back to the static bearer token. It never means "no auth in production":
    ``app.create_app`` treats an unconfigured *and* untokened server as a
    startup error.
    """
    if not settings.oauth_configured:
        return None
    if not settings.allowed_emails:
        raise AllowlistEmptyError(
            "TRAINDB_ALLOWED_EMAILS is empty while Google OAuth is enabled. "
            "Google sign-in alone authenticates but does not authorise — with no "
            "allowlist any Google account could read your health data. Set the "
            "list, or disable OAuth with TRAINDB_OAUTH_ENABLED=false."
        )

    from fastmcp.server.auth.providers.google import GoogleProvider

    class _AllowlistedGoogle(GoogleProvider):
        """GoogleProvider that additionally requires an allowlisted email."""

        async def verify_token(self, token: str) -> Any:
            # super() routes through OAuthProxy.load_access_token: it validates
            # the FastMCP JWT, swaps it for the upstream Google token, and
            # returns an AccessToken whose claims carry the Google email.
            access = await super().verify_token(token)
            if access is None:
                log.info("OAuth: upstream token validation failed")
                return None
            claims = getattr(access, "claims", None) or {}
            email = claims.get("email")
            if is_allowed(settings, email):
                return access
            log.warning("OAuth: rejected %s (not on the allowlist)", email or "<no email claim>")
            return None

    # base_url is the domain ROOT, not the /mcp path: MCP clients probe
    # /.well-known/oauth-protected-resource and /.well-known/oauth-authorization-server
    # there, and do not reliably follow path-prefixed discovery. Those two paths
    # plus /register must stay reachable WITHOUT a login, which is why no
    # Cloudflare Access policy may sit in front of this server — Access answers
    # them with an HTML login page and the client fails before OAuth begins.
    return _AllowlistedGoogle(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=settings.base_url.rstrip("/"),
        redirect_path="/auth/callback",
        required_scopes=[EMAIL_SCOPE],
        jwt_signing_key=settings.jwt_signing_key or None,
    )
