"""Wahoo OAuth: obtain, store and refresh tokens.

Wahoo issues a two-hour access token alongside a long-lived refresh token, so
the browser round-trip happens once and never again unless the refresh token is
revoked. That is the opposite of Garmin, where a credential login is expensive
and rate-limited per account — here the cost is a manual paste, not a lockout,
so this module is deliberately simpler than ``ingest.garmin.auth``.

The redirect URI must be HTTPS and must match the app registration exactly.
Wahoo does not accept a localhost callback, which is why the code arrives at the
deployed server rather than at a listener this CLI could open itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from soma.config import settings

log = logging.getLogger("soma.ingest.wahoo.auth")

# Everything the app was granted. Requested explicitly rather than relying on
# the portal's defaults, so a scope silently disappearing shows up here.
SCOPES = ("workouts_read", "plans_read", "power_zones_read", "user_read")


class NotAuthenticatedError(RuntimeError):
    """Raised when no usable Wahoo token is stored."""


def authorize_url(scopes: tuple[str, ...] = SCOPES) -> str:
    """The URL to open in a browser to grant access."""
    if not settings.wahoo_configured:
        raise NotAuthenticatedError(
            "SOMA_WAHOO_CLIENT_ID, SOMA_WAHOO_CLIENT_SECRET and "
            "SOMA_WAHOO_REDIRECT_URI must all be set."
        )
    query = urllib.parse.urlencode(
        {
            "client_id": settings.wahoo_client_id,
            "redirect_uri": settings.wahoo_redirect_uri,
            "scope": " ".join(scopes),
            "response_type": "code",
        }
    )
    return f"{settings.wahoo_api_base}/oauth/authorize?{query}"


def load_tokens(path: Path | None = None) -> dict[str, Any]:
    store = path or settings.wahoo_tokenstore
    if not store.exists():
        raise NotAuthenticatedError(
            f"No Wahoo tokens at {store}. Run `wahoo-auth --url`, open the link, "
            "then `wahoo-auth --code <code from the callback URL>`."
        )
    return json.loads(store.read_text())


def save_tokens(tokens: dict[str, Any], path: Path | None = None) -> Path:
    store = path or settings.wahoo_tokenstore
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(tokens, indent=2))
    # The refresh token is a long-lived credential. Do not leave it group- or
    # world-readable just because the data directory is not.
    store.chmod(0o600)
    return store


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    payload = {
        **payload,
        "client_id": settings.wahoo_client_id,
        "client_secret": settings.wahoo_client_secret,
    }
    response = httpx.post(f"{settings.wahoo_api_base}/oauth/token", data=payload, timeout=30)
    body = response.json()
    if response.status_code >= 400 or "error" in body:
        # Wahoo answers in whatever language it guesses, so the raw description
        # is reproduced rather than paraphrased.
        raise NotAuthenticatedError(
            f"Wahoo token request failed ({response.status_code}): "
            f"{body.get('error')} — {body.get('error_description')}"
        )
    return body


def exchange_code(code: str) -> dict[str, Any]:
    """Trade a one-time authorization code for tokens. Codes expire in minutes."""
    return _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.wahoo_redirect_uri,
        }
    )


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    return _post_token({"grant_type": "refresh_token", "refresh_token": refresh_token})


def expires_at(tokens: dict[str, Any]) -> float:
    """Absolute expiry, from Wahoo's ``created_at`` + ``expires_in``."""
    return float(tokens.get("created_at", 0)) + float(tokens.get("expires_in", 0))


def is_expired(tokens: dict[str, Any], now: float | None = None) -> bool:
    """Whether the access token is gone, or close enough that a sync would outlive it."""
    clock = time.time() if now is None else now
    return expires_at(tokens) - settings.wahoo_refresh_margin_s <= clock


def valid_access_token(path: Path | None = None, now: float | None = None) -> str:
    """A usable access token, refreshing and re-storing it when necessary."""
    tokens = load_tokens(path)
    if is_expired(tokens, now):
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise NotAuthenticatedError(
                "Access token expired and no refresh token is stored. Re-authorize."
            )
        log.info("Wahoo access token expired; refreshing.")
        tokens = refresh_tokens(refresh)
        save_tokens(tokens, path)
    return str(tokens["access_token"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Authorize soma against the Wahoo Cloud API.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", action="store_true", help="Print the authorization URL to open.")
    group.add_argument("--code", help="Authorization code from the callback URL.")
    group.add_argument("--status", action="store_true", help="Report on the stored tokens.")
    args = parser.parse_args()

    if args.url:
        print(authorize_url())
        print(
            "\nOpen that, approve, then copy the `code=` value out of the URL you "
            "land on and run:\n  wahoo-auth --code <code>"
        )
        return

    if args.status:
        tokens = load_tokens()
        remaining = int(expires_at(tokens) - time.time())
        print(f"  scopes:        {tokens.get('scope')}")
        print(f"  user_id:       {tokens.get('user_id')}")
        print(f"  access token:  {'expired' if remaining <= 0 else f'{remaining // 60} min left'}")
        print(f"  refresh token: {'present' if tokens.get('refresh_token') else 'MISSING'}")
        return

    tokens = exchange_code(args.code)
    store = save_tokens(tokens)
    log.info("Authorized as user %s. Tokens stored at %s", tokens.get("user_id"), store)


if __name__ == "__main__":
    main()
