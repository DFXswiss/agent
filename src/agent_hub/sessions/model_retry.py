"""The next attempt after the owner connected model access (ADR-079 section 4, rule E-05).

A main session whose start finds no model access fails at once with `model_access_unavailable` (05-runner.md section
10.1): the missing precondition is detected immediately. The task's chat shows its owner a card that starts the
connection of the Claude subscription. Once the owner stores a model credential (`claude_subscription` or
`anthropic_api_key`, `PUT /v1/me/credentials/{kind}`), the hub creates the next attempt of every open task of that
owner whose newest main session failed for this reason with prompts pending: a new main session whose first
transition `requested` carries `model_access_unavailable` and takes over the undelivered prompts
(`agent_hub.sessions.prompts`), exactly as the attempt after a refusal for lack of capacity (`agent_hub.sessions.retry`,
whose rules decide: newest main session of an open task, not stopped by the user, prompts pending). The task continues
by itself; nobody repeats the instruction.

- `after_credential(connection, owner, now, effects)` runs in the transaction that stores the credential (the
  credential row is no lock of the tasks area; the tasks are locked one by one, oldest failure first).
- Both take the advisory lock of the owner (`lock_owner`) before any task lock, so a credential written in the same
  moment as the failure is seen by one of them.
- `planned(connection, task, session_id, now)` runs in the transaction that stores the failure: a model credential
  stored after the failed session was created (it arrived while the start ran) gets its attempt at once; otherwise
  the next credential write creates it. So every stored credential leads to at most one attempt per task: an attempt
  that fails again for the same reason (the device could not open the credential yet) waits for the next write.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.sessions import prompts, retry
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import TaskRow, lock_task

Connection = psycopg.Connection[Any]
REASON = prompts.MODEL_RETRY_REASON
MODEL_KINDS = ("claude_subscription", "anthropic_api_key")
"""Credential kinds that give a session model access (05-runner.md section 10.1)."""

_FAILED = (
    "SELECT t.id, s.id FROM hub.task t CROSS JOIN LATERAL (SELECT id, state, state_reason, created_at FROM hub.session"
    " WHERE task_id = t.id AND depth = 0 ORDER BY created_at DESC, id DESC LIMIT 1) s"
    " WHERE t.owner_id = %s AND t.state = 'failed' AND s.state = 'failed' AND s.state_reason = %s"
    " ORDER BY s.created_at, t.id"
)


OWNER_LOCK = 742180110
"""Namespace of the advisory lock per owner (next to the hub's locks 742180100-742180102)."""


def lock_owner(connection: Connection, owner: UUID) -> None:
    """Serialise a credential write and a model-access failure of one owner (taken before any task lock), so that one
    of the two always sees the other committed: no attempt is lost when both happen at the same moment."""
    connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s::text))", (OWNER_LOCK, str(owner))
    )


def _credential_after(connection: Connection, owner: UUID, after: datetime) -> bool:
    found = connection.execute(
        "SELECT 1 FROM hub.credential WHERE owner_id = %s AND kind = ANY(%s) AND updated_at > %s",
        (owner, list(MODEL_KINDS), after),
    ).fetchone()
    return found is not None


def planned(connection: Connection, task: TaskRow, session_id: UUID, now: datetime) -> datetime | None:
    """The creation time of the attempt after `session_id` of the locked task failed for want of model access, None
    when no model credential arrived after the session was created or the capacity rules give none."""
    created = cast(
        tuple[datetime],
        connection.execute("SELECT created_at FROM hub.session WHERE id = %s", (session_id,)).fetchone(),
    )[0]
    if not _credential_after(connection, task.owner_id, created):
        return None
    return retry.planned(connection, task, session_id, now)


def after_credential(connection: Connection, owner: UUID, kind: str, now: datetime, effects: Effects) -> int:
    """Create the attempts of the owner's tasks waiting for model access after it stored `kind`; their number."""
    if kind not in MODEL_KINDS:
        return 0
    lock_owner(connection, owner)
    started = 0
    for task_id, session_id in connection.execute(_FAILED, (owner, REASON)).fetchall():
        task = cast(TaskRow, lock_task(connection, task_id))
        due = retry.planned(connection, task, session_id, now)
        if due is None:
            continue
        retry.start(connection, task, due, effects, REASON)
        started += 1
    return started
