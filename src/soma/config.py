"""Runtime configuration, loaded from environment / .env.

Everything uses the ``SOMA_`` prefix. Vendor-specific settings carry the
vendor in the name — ``SOMA_GARMIN_EMAIL``, and later
``SOMA_WAHOO_CLIENT_ID`` — so a second ingestion source adds keys rather than
reorganising them.

Three identities live here and are easy to confuse:

- ``allowed_emails`` — the Google accounts permitted to *call* this server.
- ``garmin_email`` — the Garmin account the sync worker reads *from*.
- ``google_client_id`` — the OAuth app that proves the first of those.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Project root = two levels up from this file (src/soma/config.py).
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOMA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- What "today" means ---
    # Every date in the database is a join key, so this decides which row a
    # write lands on. IANA name, e.g. America/Santiago. See soma.clock.
    timezone: str = "UTC"

    # --- Serving ---
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = DATA_DIR / "soma.db"

    # --- Who may call this server ---
    # Static bearer token, for local development and stdio.
    api_token: str = ""
    oauth_enabled: bool = True
    google_client_id: str = ""
    google_client_secret: str = ""
    # Public origin. Pinned rather than derived from x-forwarded-host, so a
    # spoofed header cannot redirect the OAuth flow.
    base_url: str = ""
    jwt_signing_key: str = ""
    allowed_emails: Annotated[set[str], NoDecode] = set()

    # --- Garmin ingestion ---
    garmin_email: str = ""
    garmin_tokenstore: Path = DATA_DIR / "garmin_tokens"
    garmin_sync_days_back: int = 365
    # Randomized pause before a credential login hits Garmin SSO.
    garmin_login_delay_min: float = 5.0
    garmin_login_delay_max: float = 15.0
    # Minimum gap between two credential logins. Garmin's SSO limit is per
    # account, so a retry loop locks the account out for hours.
    garmin_login_cooldown_s: float = 900.0
    # Randomized pause between per-day data requests during a sync.
    garmin_request_delay_min: float = 0.4
    garmin_request_delay_max: float = 1.2

    # --- Wahoo ingestion ---
    # Wahoo is the source of truth for virtual and trainer rides: Garmin records
    # none of them, which is why this exists at all. See issue #3.
    wahoo_client_id: str = ""
    wahoo_client_secret: str = ""
    # Must match a callback URL registered on the app, byte for byte. Wahoo
    # requires HTTPS, so this cannot be a localhost address.
    wahoo_redirect_uri: str = ""
    wahoo_tokenstore: Path = DATA_DIR / "wahoo_tokens.json"
    wahoo_sync_days_back: int = 365
    wahoo_api_base: str = "https://api.wahooligan.com"
    # Refresh this many seconds before the access token actually expires, so a
    # long sync cannot have one die underneath it mid-page.
    wahoo_refresh_margin_s: float = 300.0

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        """Reject an unknown zone at startup rather than at first write.

        A typo here does not raise where it is set; it raises the first time
        something asks what day it is, which on the sync worker is the middle of
        a run and on the server is the first tool call.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"SOMA_TIMEZONE={value!r} is not a known IANA timezone "
                "(expected something like 'America/Santiago' or 'UTC')."
            ) from exc
        return value

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _split_emails(cls, value: Any) -> Any:
        """Accept ``a@x.com, b@y.com`` from the environment, lowercased."""
        if isinstance(value, str):
            return {part.strip().lower() for part in value.split(",") if part.strip()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(part).strip().lower() for part in value if str(part).strip()}
        return value

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def garmin_cooldown_file(self) -> Path:
        """Where the Garmin auth CLI records its last credential-login attempt."""
        return self.garmin_tokenstore / "last_login_attempt"

    @property
    def wahoo_configured(self) -> bool:
        """Whether there is enough config to attempt a Wahoo OAuth exchange."""
        return bool(self.wahoo_client_id and self.wahoo_client_secret and self.wahoo_redirect_uri)

    @property
    def oauth_configured(self) -> bool:
        """Whether there is enough config to stand up the Google OAuth flow."""
        return bool(
            self.oauth_enabled
            and self.google_client_id
            and self.google_client_secret
            and self.base_url
        )


settings = Settings()
