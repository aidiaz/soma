"""What this system calls "today".

Every date in the database is a join key, so the answer to "what day is it"
decides which row a write lands on. That decision is made here, once, rather
than independently at each call site.

**The answer is the local date**, in the zone named by ``SOMA_TIMEZONE``.

This reverses an earlier decision, and the reversal is deliberate rather than a
correction of a mistake. UTC was chosen on 2026-08-24 with its cost written
down: a meal or a ride logged late in the evening, in a zone west of UTC, is
recorded against the following day. That was an acceptable trade while the
system held training data, where it bites occasionally. Intake tracking makes it
constant — evening is when people eat, and a dinner logged at 21:00 in Santiago
landing on tomorrow would corrupt the daily totals it exists to produce.

What UTC bought was a boundary identical everywhere. That is still available and
still used: ``now()`` is an aware UTC instant, and stored timestamps stay
absolute. Only the *calendar day* is local, which is the thing a human means by
"today".

This is not the same question as which day an activity belongs to. Wahoo reports
an instant plus the zone it happened in, and ``ingest.wahoo.sync`` derives the
date from that, so a ride done while travelling counts on the day it happened
*there*. That boundary is the ride's, not the server's, and it is correct that
the two can differ.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from soma.config import settings

# Named rather than inlined, so a grep for what this system assumes lands here.
UTC = dt.UTC


def now() -> dt.datetime:
    """The current instant, timezone-aware, in UTC.

    Absolute and unambiguous. Use this for anything stored as a timestamp; use
    :func:`today` for anything used as a join key.
    """
    return dt.datetime.now(tz=UTC)


def local_zone() -> ZoneInfo:
    """The configured zone. Validated at settings load, so this cannot raise."""
    return ZoneInfo(settings.timezone)


def today() -> dt.date:
    """The current date where the athlete is."""
    return now().astimezone(local_zone()).date()
