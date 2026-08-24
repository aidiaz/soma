"""Probe every third-party endpoint this system depends on. No credentials needed.

Answers one question before a deploy: can this host actually reach the services,
and are they up? It deliberately does *not* authenticate — a 401 or 400 from an
endpoint that requires credentials is a pass, because it proves the service
answered.

    make reach

Run it from a new network before blaming the code: a Pi behind a captive portal
and a laptop on a VPN fail here in ways that look like application bugs later.
"""

from __future__ import annotations

import sys
from typing import NamedTuple

import httpx

TIMEOUT = 15


class Probe(NamedTuple):
    group: str
    name: str
    url: str
    # Status codes that prove the service answered. An endpoint needing auth
    # answers 401; a POST-only endpoint answers 404/405 to a GET. Both are
    # reachability passes — only a transport failure is a real one.
    ok: tuple[int, ...]
    note: str = ""


PROBES = [
    # Garmin — the fragile source, and the only one for sleep, HRV, Body
    # Battery and resting HR. Reached through curl_cffi by garminconnect.
    Probe("garmin", "SSO sign-in", "https://sso.garmin.com/sso/signin", (200,)),
    Probe("garmin", "Connect web", "https://connect.garmin.com/", (200, 302)),
    Probe(
        "garmin",
        "Connect API",
        "https://connectapi.garmin.com/userprofile-service/socialProfile",
        (401, 403),
        "403 unauthenticated is correct — it proves the API answered",
    ),
    # Google — proves the identity of anyone calling the MCP server.
    Probe(
        "google",
        "OpenID discovery",
        "https://accounts.google.com/.well-known/openid-configuration",
        (200,),
    ),
    Probe("google", "Token endpoint", "https://oauth2.googleapis.com/token", (400, 404, 405)),
    Probe(
        "google",
        "Tokeninfo",
        "https://www.googleapis.com/oauth2/v3/tokeninfo",
        (400,),
        "400 without a token is correct",
    ),
    # Wahoo — not yet ingested. Probed so Phase 1 does not start by debugging
    # connectivity.
    Probe("wahoo", "Cloud API", "https://api.wahooligan.com/v1/user", (401,)),
    Probe(
        "wahoo",
        "OAuth authorize",
        "https://api.wahooligan.com/oauth/authorize",
        (200, 302, 400),
        "302 with no client_id is correct — it redirects to the error page",
    ),
    # Cloudflare — the tunnel dials out to this edge.
    Probe(
        "cloudflare",
        "API",
        "https://api.cloudflare.com/client/v4/user/tokens/verify",
        (400, 401, 403),
    ),
]


def probe(p: Probe) -> tuple[bool, str]:
    try:
        response = httpx.get(p.url, timeout=TIMEOUT, follow_redirects=False)
    except Exception as exc:  # noqa: BLE001 - any transport failure is the finding
        return False, f"unreachable: {type(exc).__name__}: {str(exc)[:60]}"
    if response.status_code in p.ok:
        return True, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code} (expected one of {p.ok})"


def check_garmin_transport() -> tuple[bool, str]:
    """Confirm garminconnect's HTTP client is importable and can reach Garmin.

    garminconnect drives Garmin through curl_cffi, which impersonates a browser
    TLS fingerprint. Whether plain requests/httpx would *also* work is not
    something this can settle without credentials — the login POST is where bot
    detection bites, not the pages probed above. The check that matters is that
    the client the library actually uses works from here.
    """
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return False, "curl_cffi is not installed — garminconnect cannot work"
    try:
        response = cr.get(
            "https://sso.garmin.com/sso/signin", impersonate="chrome", timeout=TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"curl_cffi cannot reach Garmin SSO: {type(exc).__name__}"
    return response.status_code == 200, f"curl_cffi HTTP {response.status_code}"


def main() -> int:
    failures = 0
    group = None
    for p in PROBES:
        if p.group != group:
            group = p.group
            print(f"\n{group}")
        ok, detail = probe(p)
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {p.name:20} {detail}")
        if p.note and ok:
            print(f"         {p.note}")
        failures += not ok

    print("\ntransport")
    ok, detail = check_garmin_transport()
    print(f"  [{'ok  ' if ok else 'FAIL'}] {'curl_cffi -> Garmin':20} {detail}")
    failures += not ok

    if failures:
        print(f"\n{failures} probe(s) failed. Check the network before deploying.", file=sys.stderr)
        return 1
    print("\nAll endpoints reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
