"""The nonce replay store of the signature gate (03-api.md section 1, 02-data-model.md section 2.1 `request_nonce`).

A nonce is accepted once per key ID within the replay window of 600 s (the time window of 120 s refuses anything
older). Rows older than the window are deleted on every insert (legacy rule) and by the purge job (`http/jobs.py`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.db.pool import transaction

NONCE_WINDOW_SECONDS = 600


def _cutoff(now: datetime) -> datetime:
    return now - timedelta(seconds=NONCE_WINDOW_SECONDS)


def insert_nonce(pool: ConnectionPool[psycopg.Connection[Any]], key_id: str, nonce: str, now: datetime) -> bool:
    """Record a nonce; False when the key ID used it within the window (a replay)."""
    with transaction(pool) as connection:
        connection.execute("DELETE FROM hub.request_nonce WHERE seen_at < %s", (_cutoff(now),))
        inserted = connection.execute(
            "INSERT INTO hub.request_nonce (key_id, nonce, seen_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (key_id, nonce) DO NOTHING RETURNING 1",
            (key_id, nonce, now),
        ).fetchone()
    return inserted is not None


def purge_nonces(pool: ConnectionPool[psycopg.Connection[Any]], now: datetime) -> int:
    """Delete the rows older than the window; the number of deleted rows."""
    with transaction(pool) as connection:
        return connection.execute("DELETE FROM hub.request_nonce WHERE seen_at < %s", (_cutoff(now),)).rowcount
