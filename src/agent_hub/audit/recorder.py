"""The audit rows a request appended (ADR-063 section 1).

The request pipeline runs every write request under `recording()`: `append_audit` notes `(seq, hash, result)` of every
row it appends while the request is processed (also in worker threads: `asyncio.to_thread` and the thread pool copy
the context, and the recording is shared). A handler that answers an idempotent repeat without changing anything calls
`unchanged()`: the pipeline's generic row is then the audit of that request, not a defect; one that stored what a job
audits later calls `deferred()`, and a success needs no row of the pipeline. A noted row may still be
rolled back with the route's transaction, so `committed` asks the database which of them exist with the same hash: a
later row that took a rolled-back `seq` has a different hash.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import psycopg


@dataclass(frozen=True)
class Noted:
    """One row appended while a request was processed."""

    seq: int
    hash: str
    result: str


@dataclass
class Recording:
    """What one write request appended, and whether its handler declared it a repeat without change."""

    rows: list[Noted] = field(default_factory=list)
    unchanged: bool = False
    deferred: bool = False


RECORDER: ContextVar[Recording | None] = ContextVar("agent_hub_audit_recorder", default=None)


def note(seq: int, digest: str, result: str) -> None:
    """Note an appended row for the request being processed (nothing outside a request)."""
    found = RECORDER.get()
    if found is not None:
        found.rows.append(Noted(seq=seq, hash=digest, result=result))


def unchanged() -> None:
    """The request being processed is an idempotent repeat that changed nothing (nothing outside a request)."""
    found = RECORDER.get()
    if found is not None:
        found.unchanged = True


def deferred() -> None:
    """The request being processed stored something whose audit row a job writes later (the session's own events,
    `agent_hub.audit.eventbatch`); nothing outside a request."""
    found = RECORDER.get()
    if found is not None:
        found.deferred = True


@contextmanager
def recording() -> Iterator[Recording]:
    """Collect the rows appended inside the block (by this context and the contexts copied from it)."""
    found = Recording()
    token = RECORDER.set(found)
    try:
        yield found
    finally:
        RECORDER.reset(token)


def committed(connection: psycopg.Connection[Any], noted: Sequence[Noted]) -> list[Noted]:
    """The noted rows that are stored (same `seq` and hash), in the order they were noted."""
    if not noted:
        return []
    stored = {
        (int(seq), str(digest))
        for seq, digest in connection.execute(
            "SELECT seq, hash FROM hub.audit WHERE seq = ANY(%s)", ([row.seq for row in noted],)
        )
    }
    return [row for row in noted if (row.seq, row.hash) in stored]
