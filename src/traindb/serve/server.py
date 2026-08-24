"""MCP tool definitions and the stdio entry point.

Six tools, not thirty. One person, one read surface. Every tool reads or writes
only the local database — none of them contacts Wahoo or Garmin. That is
deliberate:

- only the sync workers can trip a vendor rate limit;
- tool calls stay fast, with no network in the path;
- when a vendor changes its API and ingestion breaks, the server keeps serving
  the history already stored.

Do not add a tool that calls a vendor API.

:func:`build_mcp` is a factory, not a module-level singleton. The HTTP entry
point (:mod:`traindb.serve.app`) used to mutate a shared instance, which
silently broke the stdio server; each entry point now gets its own.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from traindb.db import init_db
from traindb.serve import queries

log = logging.getLogger("traindb.serve.server")

SCOPE = "read:traindb"


def build_mcp(auth: Any = None) -> FastMCP:
    """Build a fresh FastMCP instance with the training-data tools.

    ``auth`` is a FastMCP auth provider, or ``None`` for stdio, where the
    transport is a local pipe and the OS process boundary is the gate.
    """
    mcp = FastMCP(name="traindb", auth=auth)

    # --- read ---------------------------------------------------------------

    @mcp.tool
    def get_training_week(week_start: str | None = None) -> dict:
        """The merged week — the primary tool. Answers "here is my week, what should change?"

        Returns sessions with TSS and power, daily sleep/HRV/resting-HR, logged
        food, body measurements, and a `signals` block holding the derived
        numbers the coaching rules key on: average intake, TSS ramp against the
        prior week, CTL/ATL/TSB, and week-over-week changes in resting HR and HRV.

        `week_start` is any ISO date inside the week you want; it resolves to
        that week's Monday. Defaults to the current week.

        Check `coverage` before reading anything into a gap — a sync that failed
        looks exactly like a week of rest days.
        """
        return queries.get_training_week(week_start=week_start)

    @mcp.tool
    def get_health_trend(days: int = 14) -> dict:
        """Resting HR, HRV, sleep and Body Battery over the last `days` days, newest first.

        Includes three-day averages against the rest of the window, which is the
        shape the multi-day recovery rules need — a single reading cannot show a
        trend.
        """
        return queries.get_health_trend(days=days)

    @mcp.tool
    def get_recent_activities(n: int = 10, include_raw: bool = False) -> list[dict]:
        """The last `n` sessions with power, HR, duration and TSS, newest first.

        For when one specific ride needs looking at. Set include_raw=True for the
        untouched vendor payload.
        """
        return queries.get_recent_activities(n=n, include_raw=include_raw)

    @mcp.tool
    def get_tests() -> list[dict]:
        """FTP and 4DP test history, newest first, with W/kg. The scoreboard."""
        return queries.get_tests()

    # --- write — narrow and deliberate --------------------------------------

    @mcp.tool
    def log_nutrition(
        day: str | None = None,
        kcal: int | None = None,
        protein_g: float | None = None,
        fat_g: float | None = None,
        carbs_g: float | None = None,
        creatine: bool = False,
        magnesium: bool = False,
        vitamin_d: bool = False,
        probiotic: bool = False,
        note: str | None = None,
    ) -> dict:
        """Record a day's food and supplements.

        No sensor knows what you ate, so this half of the record is permanently
        manual. Reporting a day's food in conversation is what should create the
        entry — the gap this closes is the one where the log depended on
        remembering to type it in later.

        `day` is an ISO date, defaulting to today. Upserts: logging the same day
        again corrects it. There is no delete.
        """
        return queries.log_nutrition(
            day=day,
            kcal=kcal,
            protein_g=protein_g,
            fat_g=fat_g,
            carbs_g=carbs_g,
            creatine=creatine,
            magnesium=magnesium,
            vitamin_d=vitamin_d,
            probiotic=probiotic,
            note=note,
        )

    @mcp.tool
    def log_body(
        day: str | None = None,
        weight_kg: float | None = None,
        waist_cm: float | None = None,
    ) -> dict:
        """Record weight and waist. Weekly cadence; waist is the primary measure.

        `day` is an ISO date, defaulting to today. Upserts on the date.
        """
        return queries.log_body(day=day, weight_kg=weight_kg, waist_cm=waist_cm)

    return mcp


def main() -> None:
    """Run over stdio, for a local `claude mcp add` style connection."""
    logging.basicConfig(level=logging.INFO)
    init_db()
    build_mcp().run()


if __name__ == "__main__":
    main()
