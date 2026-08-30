"""The on-demand sync queue. Written by the server, read by the workers.

Ingestion is scheduled: Garmin at 08:00, Wahoo hourly. That is right for the
data and wrong for the moment you step off the trainer and want to look at the
ride. The obvious fix — an MCP tool that syncs — is the one thing the server may
not do, because a tool that reaches a vendor puts vendor credentials, vendor
rate limits and vendor outages inside the request path of every question asked
of this database.

So the tool asks and a worker answers. The server appends a row here; the
worker for that source is already awake between runs waiting on exactly this
table (:mod:`soma.ingest.schedule`) and starts early when it sees one. Each
side keeps what it already had — the server its database-only surface, the
worker its sole claim on the vendor credential.

This module sits at the top level rather than in ``serve/`` or ``ingest/``
because both layers use it and neither may import the other, which is the same
argument that keeps :data:`soma.models.SYNC_SOURCES` in ``models``.

**Requests coalesce.** Three asks while a sync is pending are one sync. Asking
is cheap and idempotent by design, because the caller is usually a language
model that has no way to know whether its previous call already covered this.
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import col, select

from soma.clock import now
from soma.db import get_session
from soma.models import SyncRequest


def _pending_stmt(source: str):
    return (
        select(SyncRequest)
        .where(col(SyncRequest.source) == source, col(SyncRequest.served_at).is_(None))
        .order_by(col(SyncRequest.requested_at))
    )


def pending(source: str) -> SyncRequest | None:
    """The oldest unserved request for ``source``, or ``None``.

    Opens and closes a session per call, which matters because the caller is a
    poll loop that runs for hours. A session held open across the wait would
    keep a read transaction open with it, and a read transaction that never ends
    stops WAL from ever checkpointing — an unbounded ``-wal`` file on an SD
    card. The connection itself is pooled, so the repeated open is nearly free.
    """
    with get_session() as session:
        row = session.exec(_pending_stmt(source)).first()
        if row is not None:
            session.expunge(row)
        return row


def enqueue(source: str, note: str | None = None) -> tuple[SyncRequest, bool]:
    """Ask ``source`` to sync. Returns the request and whether it is a new one.

    An unserved request already exists is the common case, not an error: the
    queue's job is to carry "sync soon", and that is fully expressed by one row.
    A second row would buy a second sync of the same data.
    """
    with get_session() as session:
        existing = session.exec(_pending_stmt(source)).first()
        if existing is not None:
            session.expunge(existing)
            return existing, False
        row = SyncRequest(source=source, requested_at=now().replace(tzinfo=None), note=note)
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row, True


def mark_served(source: str, run_id: int | None, before: dt.datetime) -> int:
    """Mark every request for ``source`` made at or before ``before`` as served.

    ``before`` is the run's own start. A request made *while* a sync was running
    is deliberately left pending: that sync had already decided what to fetch
    before the ask existed, so serving it would answer a question nobody got to
    ask. The cost is one extra run; the alternative is silently dropping a
    request, and of those two only one is discoverable by the person waiting.

    Returns how many rows it closed.
    """
    stamp = now().replace(tzinfo=None)
    with get_session() as session:
        rows = session.exec(
            _pending_stmt(source).where(col(SyncRequest.requested_at) <= before)
        ).all()
        for row in rows:
            row.served_at = stamp
            row.run_id = run_id
            session.add(row)
        session.commit()
        return len(rows)
