"""Interactive Garmin login CLI (run rarely, ~every 6 months).

Reuses existing tokens if still valid; otherwise performs one credential login
(handling MFA) and saves OAuth tokens to the token store. Your password is never
written to disk.

Garmin's SSO rate limit is per account, not per IP. Changing network or user
agent does nothing — only time clears it, and a lockout can last hours. So a
credential login is gated twice: a persisted cooldown that survives process
restarts, then a randomized delay before the request itself.
"""

from __future__ import annotations

import getpass
import random
import sys
import time

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from traindb.config import settings


class CooldownActive(RuntimeError):
    """Raised when a credential login is attempted inside the cooldown window."""

    def __init__(self, remaining_s: float) -> None:
        self.remaining_s = remaining_s
        super().__init__(f"Login cooldown active: {remaining_s:.0f}s remaining.")


def _tokenstore() -> str:
    settings.garmin_tokenstore.mkdir(parents=True, exist_ok=True)
    return str(settings.garmin_tokenstore)


def last_attempt_at() -> float | None:
    """Epoch seconds of the last credential-login attempt, or None."""
    path = settings.garmin_cooldown_file
    if not path.exists():
        return None
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        # An unreadable marker must not be treated as "no recent attempt" —
        # falling through to a login is the outcome the cooldown exists to
        # prevent. Report it as an attempt that just happened.
        return time.time()


def cooldown_remaining(now: float | None = None) -> float:
    """Seconds left before another credential login may be attempted."""
    last = last_attempt_at()
    if last is None:
        return 0.0
    elapsed = (time.time() if now is None else now) - last
    return max(0.0, settings.garmin_login_cooldown_s - elapsed)


def record_attempt(now: float | None = None) -> None:
    """Stamp the cooldown marker.

    Called *before* contacting Garmin, not after: a login that crashes or is
    interrupted still consumed an attempt against the account's limit.
    """
    settings.garmin_tokenstore.mkdir(parents=True, exist_ok=True)
    settings.garmin_cooldown_file.write_text(str(time.time() if now is None else now))


def _try_token_reuse(tokenstore: str) -> bool:
    try:
        garmin = Garmin()
        garmin.login(tokenstore)
        name = garmin.get_full_name() or getattr(garmin, "display_name", "your account")
        print(f"✓ Existing tokens are still valid (logged in as {name}). Nothing to do.")
        return True
    except Exception:  # noqa: BLE001 - any failure just means we must log in fresh
        return False


def main() -> None:
    tokenstore = _tokenstore()
    print(f"Token store: {tokenstore}")

    if _try_token_reuse(tokenstore):
        return

    remaining = cooldown_remaining()
    if remaining > 0:
        print(
            f"\n✗ A credential login was attempted {settings.garmin_login_cooldown_s - remaining:.0f}s "
            f"ago. Wait {remaining / 60:.1f} more minutes.\n"
            "  Garmin rate-limits per account; retrying sooner extends the lockout "
            "rather than clearing it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nNo valid tokens found — performing a fresh Garmin login.")
    print("⚠️  Garmin rate-limits logins PER ACCOUNT. Do NOT run this in a loop, or")
    print("    you may lock yourself out for minutes-to-hours. Switching network/IP")
    print("    does NOT help. Once tokens are saved, `garmin-sync` reuses them.\n")

    email = settings.garmin_email or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden): ")
    if not email or not password:
        print("✗ Email and password are required.", file=sys.stderr)
        sys.exit(2)

    record_attempt()

    delay = random.uniform(settings.garmin_login_delay_min, settings.garmin_login_delay_max)
    print(f"Waiting {delay:.0f}s before contacting Garmin SSO (Cloudflare courtesy delay)...")
    time.sleep(delay)

    garmin = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Enter MFA / 2FA code: ").strip(),
    )
    try:
        garmin.login(tokenstore)
    except GarminConnectTooManyRequestsError:
        print(
            "\n✗ Garmin returned 429 Too Many Requests. Wait several minutes "
            "before retrying — do not loop.",
            file=sys.stderr,
        )
        sys.exit(1)
    except GarminConnectAuthenticationError as exc:
        print(f"\n✗ Authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)

    name = garmin.get_full_name() or getattr(garmin, "display_name", "your account")
    print(f"\n✓ Logged in as {name}. Tokens saved to {tokenstore}.")
    print("  Next: run `uv run garmin-sync` to pull your data.")


if __name__ == "__main__":
    main()
