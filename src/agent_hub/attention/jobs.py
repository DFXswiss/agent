"""The jobs of the attention area (ADR-076 section 4, ADR-027 section 4 `<area>/jobs.py:JOBS`).

`attention.backfill` runs every `INTERVAL_SECONDS` and derives the record of every task that has none (the tasks
created before migration 0017, a lost row, a captured task of before ADR-103) with
`agent_hub.attention.store.refresh`, at most `BATCH` tasks per
transaction (each takes the audit lock, so a batch is kept short), until none is left. It is idempotent: a task with
a record is never selected, and a refresh of the same state writes nothing. A backfilled record announces nothing
(no frame, no notification): it describes a state the users already knew, and the clients read it with the lists.

`attention.recheck` re-derives, every `INTERVAL_SECONDS`, the records of the tasks that wait for a device (task state
`scheduling`): their category also depends on the workgroup's device assignments and the device's presence
(`device_missing`, `device_unreachable`, `queued`), which change without a write to the task. A changed record is
announced like any refresh (frame, notification after the commit); an unchanged one writes nothing. Each task
has its own short transaction (the refresh holds the global audit lock), and the backfill batches are small for the
same reason.
"""

from __future__ import annotations

from typing import Any

from agent_hub.attention import store
from agent_hub.jobs import Job
from agent_hub.sessions.effects import Effects

INTERVAL_SECONDS = 60.0
BATCH = 20


def _batch(context: Any) -> int:
    now = context.clock()
    with context.pool.connection() as connection, connection.transaction():
        found = connection.execute(
            "SELECT t.id FROM hub.task t WHERE NOT EXISTS (SELECT 1 FROM hub.task_attention a WHERE a.task_id = t.id)"
            " ORDER BY t.id LIMIT %s",
            (BATCH,),
        ).fetchall()
        for (task_id,) in found:
            store.refresh(connection, task_id, now, Effects())
    return len(found)


def backfill(context: Any) -> int:
    """Derive every missing record (module docstring); how many were derived."""
    total = 0
    while True:
        count = _batch(context)
        total += count
        if count < BATCH:
            return total


def recheck(context: Any) -> int:
    """Re-derive the records of the tasks waiting for a device (module docstring); how many were looked at."""
    with context.pool.connection() as connection:
        with connection.transaction():
            found = connection.execute("SELECT id FROM hub.task WHERE state = 'scheduling' ORDER BY id").fetchall()
        for (task_id,) in found:
            effects = Effects()
            with connection.transaction():
                store.refresh(connection, task_id, context.clock(), effects)
            effects.flush()
    return len(found)


JOBS = (
    Job("attention.backfill", INTERVAL_SECONDS, backfill, initial_delay_seconds=5.0),
    Job("attention.recheck", INTERVAL_SECONDS, recheck, initial_delay_seconds=30.0),
)
