"""The backfill job of the attention area (ADR-064 section 4, ADR-027 section 4 `<area>/jobs.py:JOBS`).

`attention.backfill` runs every `INTERVAL_SECONDS` and derives the record of every task that has none (the tasks
created before migration 0011, a lost row) with `agent_hub.attention.store.refresh`, at most `BATCH` tasks per
transaction (each takes the audit lock, so a batch is kept short), until none is left. It is idempotent: a task with
a record is never selected, and a refresh of the same state writes nothing. A backfilled record announces nothing
(no frame, no notification): it describes a state the users already knew, and the clients read it with the lists.
"""

from __future__ import annotations

from typing import Any

from agent_hub.attention import store
from agent_hub.jobs import Job
from agent_hub.sessions.effects import Effects

INTERVAL_SECONDS = 60.0
BATCH = 200


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


JOBS = (Job("attention.backfill", INTERVAL_SECONDS, backfill, initial_delay_seconds=5.0),)
