"""Authenticated HTTP access to the Wahoo Cloud API.

Refreshes the access token when it is close to expiry rather than waiting for a
401, so a long backfill cannot have its credential die between pages.

Unlike the Garmin client this never risks a lockout — Wahoo's limit is ordinary
rate limiting, not a per-account SSO cooldown — so a failure here is cheap to
retry and the module stays correspondingly simple.
"""

from __future__ import annotations

import logging
from typing import Any, Self

import httpx

from soma.config import settings
from soma.ingest.wahoo.auth import valid_access_token

log = logging.getLogger("soma.ingest.wahoo.client")

# Wahoo caps page size; 50 is what the API returns for a larger request anyway.
PAGE_SIZE = 50
# A runaway pager would hammer a third party. 40 pages is 2000 workouts, far
# beyond any real history, so hitting this means a bug rather than a big account.
MAX_PAGES = 40


class WahooClient:
    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        self._token = token or valid_access_token()
        self._client = httpx.Client(
            base_url=settings.wahoo_api_base,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, path: str, **params: Any) -> Any:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def workouts(self, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
        """Every workout, scheduled and completed, newest pages first.

        Returned in one list rather than streamed: the endpoint reports a total
        in the low hundreds for a personal account, and holding it in memory is
        simpler than a generator nobody needs.
        """
        out: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for page in range(1, MAX_PAGES + 1):
            payload = self.get("/v1/workouts", page=page, per_page=page_size)
            batch = payload.get("workouts", payload) if isinstance(payload, dict) else payload
            if not batch:
                break
            fresh = [w for w in batch if w.get("id") not in seen]
            seen.update(w.get("id") for w in batch)
            out.extend(fresh)
            if len(batch) < page_size:
                break
        else:
            log.warning("Stopped paging Wahoo workouts at the %d-page cap.", MAX_PAGES)
        return out


def get_client() -> WahooClient:
    return WahooClient()
