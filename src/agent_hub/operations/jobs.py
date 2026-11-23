"""The expiry job of the operations area (03-api.md section 12, ADR-027 section 4 `<area>/jobs.py:JOBS`).

`operations.expire` runs every `INTERVAL_SECONDS` and ends operations that cannot finish any more:

- `received` for `RECEIVED_LIMIT` (10 min after the session's request time, `created_at`) - `expired` with
  `hub_unreachable` (a registration is decided in its own transaction, so this is a safety net);
- `accepted` for `ACCEPTED_LIMIT` (30 min after the decision) without `executing` - `expired` with `timeout`.

Each expiry writes the operation row (`finished_at`), a transition (`by_id` NULL = the hub), releases the dedupe slot,
writes the audit row `operation.expired` (actor: the hub) and, after the commit, sends the `operation` frame and the
signal `operation.finished`. A later `operation_transition` of the runner for such an operation is answered by the
outbox's state rules (`expired` again changes nothing; any other step is refused as `rejected`).

The job takes the global audit lock **before** it locks operation rows and skips operation rows another transaction
holds (`FOR UPDATE SKIP LOCKED`), so it never waits for an operation row. The slot of an operation it locked can only
be held by a transaction that holds the audit lock (a registration claiming the slot) or that operation's row (the
outbox base handler), so the job never waits in a cycle. At most `BATCH` operations expire per transaction; the job
repeats until none is left.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_hub.audit.chain import AUDIT_LOCK
from agent_hub.jobs import Job
from agent_hub.operations import dedupe
from agent_hub.operations.steps import FINISHED_SIGNAL, OperationRef, add_transition, audit_step
from agent_hub.outbox.handlers_operation import operation_row
from agent_hub.sessions.effects import Effects

INTERVAL_SECONDS = 30.0
RECEIVED_LIMIT = timedelta(minutes=10)
ACCEPTED_LIMIT = timedelta(minutes=30)
BATCH = 100
CODES = {"received": "hub_unreachable", "accepted": "timeout"}


def _expire_batch(context: Any) -> int:
    now = context.clock()
    effects = Effects()
    with context.pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
        found = connection.execute(
            "SELECT id, org_id, wg_id, task_id, session_id, device_id, type, state FROM hub.operation"
            " WHERE (state = 'received' AND created_at <= %s) OR (state = 'accepted' AND decided_at <= %s)"
            " ORDER BY created_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (now - RECEIVED_LIMIT, now - ACCEPTED_LIMIT, BATCH),
        ).fetchall()
        for row in found:
            ref = OperationRef(*row[:7])
            code = CODES[row[7]]
            connection.execute(
                "UPDATE hub.operation SET state = 'expired', error_code = %s, finished_at = %s WHERE id = %s",
                (code, now, ref.id),
            )
            add_transition(connection, ref.id, "expired", code, at=now, by_id=None)
            dedupe.release(connection, ref.id)
            audit_step(connection, ref, "expired", now, actor_kind="hub", actor_id=None, error_code=code)
            effects.org_frame(ref.org_id, "operation", operation_row(connection, ref.id))
            effects.signal(
                FINISHED_SIGNAL,
                operation_id=ref.id,
                session_id=ref.session_id,
                task_id=ref.task_id,
                org_id=ref.org_id,
                state="expired",
            )
    effects.flush()
    return len(found)


def expire(context: Any) -> int:
    """Expire every overdue operation (module docstring); how many expired."""
    total = 0
    while True:
        count = _expire_batch(context)
        total += count
        if count < BATCH:
            return total


JOBS = (Job("operations.expire", INTERVAL_SECONDS, expire, initial_delay_seconds=30.0),)
