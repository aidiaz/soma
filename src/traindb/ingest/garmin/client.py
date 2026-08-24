"""Authenticated Garmin client for the sync worker.

Loads OAuth tokens from the token store and NEVER falls back to a credential
login (that would risk the per-account 429 lockout). If tokens are missing or
the refresh token has expired, this raises so the worker fails loudly while the
MCP server keeps serving whatever is already in SQLite.
"""

from __future__ import annotations

from garminconnect import Garmin

from traindb.config import settings


class NotAuthenticatedError(RuntimeError):
    """Raised when saved tokens are missing or no longer valid."""


def get_client() -> Garmin:
    tokenstore = str(settings.garmin_tokenstore)
    garmin = Garmin()  # no credentials -> token-only login, no SSO password POST
    try:
        garmin.login(tokenstore)
    except Exception as exc:
        raise NotAuthenticatedError(
            f"Could not authenticate from saved tokens at {tokenstore!r}. "
            "Tokens are missing or expired — run `garmin-auth` (in the container: "
            "docker compose -f compose.pi.yaml run --rm garmin-sync garmin-auth)."
        ) from exc
    return garmin
