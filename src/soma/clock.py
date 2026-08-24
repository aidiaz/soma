"""What this system calls "today".

Every date in the database is a join key, so the answer to "what day is it"
decides which row a write lands on. That decision is made here, once, rather
than independently at each call site — which is how it drifted before: nothing
set a timezone, so the answer was whatever zone the process happened to run in.
A container defaults to UTC and a laptop does not, and the same payload landed
on different days.

**The answer is UTC, deliberately.** Decided 2026-08-24, with the consequence
understood and accepted: the training day rolls over at midnight UTC, so a meal
or a ride logged late in the evening in a zone west of UTC is recorded against
the following day. What it buys is a boundary that is identical on the Pi, in a
container and on a laptop in another country, and that does not move twice a
year with daylight saving.

Changing it later is a one-line change here plus ``TZ`` in ``compose.pi.yaml``.
Every caller already routes through this module, so nothing else has to move.

This is *not* the same question as which day an activity belongs to. Garmin
reports ``startTimeLocal``, and ``ingest.garmin.sync`` deliberately keeps that
as a naive wall-clock reading so a 23:30 ride counts as that day's training.
That boundary is the ride's, not the server's.
"""

from __future__ import annotations

import datetime as dt

# Named rather than inlined, so the grep for what this system assumes lands here.
UTC = dt.UTC


def now() -> dt.datetime:
    """The current instant, timezone-aware, in UTC."""
    return dt.datetime.now(tz=UTC)


def today() -> dt.date:
    """The current date, in UTC. See the module docstring for why UTC."""
    return now().date()
