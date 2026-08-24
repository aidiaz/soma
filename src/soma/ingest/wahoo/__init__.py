"""Wahoo ingestion: the source of truth for virtual and trainer rides.

Separate from ``ingest.garmin`` on purpose. The two vendors fail independently,
and the whole point of splitting by layer rather than by vendor is that Garmin
breaking must not stop Wahoo. Nothing in ``serve/`` imports either.
"""
