"""The next attempt after a device refused a session for lack of capacity (05-runner.md section 3.4: "fail the request
with `capacity_exhausted` (the hub then re-schedules)"; 03-api.md section 4.6.2; KOR-14, ADR-049).

The hub's view of a device's capacity can lag behind (heartbeat every 15 s, pool refill, several starts in one
heartbeat window), so a device it placed a session on may have no free VM slot any more; the runner then fails the
start (or a resume that needs its VM restarted) `failed` with `capacity_exhausted`. A runner never starts a session ID
twice and `failed` is final (02-data-model.md section 2.4.1), so the hub re-schedules by a **new attempt**: in the
transaction that stores the refusal (`agent_hub.sessions.transitions.apply`) it creates the next main session of the
task, whose first transition `requested` carries the reason `capacity_exhausted` (audit `session.requested` by the
hub with that reason). The prompts the refused session did not deliver are routed to the attempt
(`agent_hub.sessions.prompts.route`), so each one is delivered exactly once. The scheduler places the attempt; it does
not count the refusing device as free until its next heartbeat (`agent_hub.scheduler.load`), so the attempt goes to
another device of the workgroup with a free slot or waits with the task `scheduling` (`capacity_exhausted`) until a
device reports capacity (`agent_hub.scheduler.reschedule`). The task is derived once, after the attempt exists: it
never shows `failed` in between.

`planned` decides whether a refusal gets an attempt and when it is created (`None`: the refusal stays the session's
end, the task fails with the reason and shows "Retry"):

- the refused session is the **newest main session** of an **open** task (a refused child session, or a main session
  a follow-up already replaced, starts nothing);
- the user did not stop it (audit `session.control` `stop`, like `agent_hub.sessions.prompts.carry_over`);
- prompts are pending there (a resume the device started on its own has nothing to deliver);
- **limit** (ADR-075 section 7, amending ADR-049): from `AGENT_HUB_CAPACITY_REFUSALS` refusals in a row for the same
  prompts on (`refusals`: the refused session and the attempts directly before it that were refused before they ever
  worked; a follow-up or a session that worked starts a new row) the attempt is still created, but it waits
  (`capacity_exhausted`) and is not placed before `AGENT_HUB_REFUSAL_HOLD_SECONDS` (`session.not_before`, honoured
  by `agent_hub.scheduler.reschedule`); the task never fails for want of capacity. Reaching the limit is logged
  (`agent_hub.sessions.retry`, `capacity_refusal_limit`).

The attempt is created after every pending prompt and after the refused session (`created_at` order of the routing);
it is triggered by the author of the first prompt it takes over (`triggered_by`, ADR-083 section 2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.ids import uuid7
from agent_hub.scheduler import load, settings
from agent_hub.scheduler.core import CAPACITY_EXHAUSTED, Decision, NoPlacement
from agent_hub.sessions import prompts
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.lifecycle import create_main_session
from agent_hub.sessions.taskstate import HUB, TaskRow
from agent_hub.usage import binding

Connection = psycopg.Connection[Any]
LOG = logging.getLogger("agent_hub.sessions.retry")
REASON = prompts.RETRY_REASON
HELD = NoPlacement(CAPACITY_EXHAUSTED)


def refusals(newest: list[tuple[Any, ...]]) -> int:
    """The refusals in a row for the same prompts, this one included: the refused session (newest first) and, while a
    session is an attempt, its predecessor when that one was refused before it ever worked. A session that worked
    before it was refused (a resume) starts a new row."""
    if not newest[0][4]:
        return 1
    count = 1
    while count < len(newest) and newest[count - 1][2] == REASON and newest[count][3]:
        count += 1
    return count


@dataclass(frozen=True)
class Attempt:
    """The next attempt: its creation time and the author of the first prompt it takes over."""

    created: datetime
    triggered_by: UUID


def _newest(connection: Connection, task_id: UUID, limit: int) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT s.id, s.created_at, (SELECT t.reason FROM hub.session_transition t WHERE t.session_id = s.id"
        " AND t.seq = 1), s.state = 'failed' AND s.state_reason = 'capacity_exhausted' AND s.first_working_at IS NULL,"
        " s.first_working_at IS NULL FROM hub.session s WHERE s.task_id = %s AND s.depth = 0"
        " ORDER BY s.created_at DESC, s.id DESC LIMIT %s",
        (task_id, limit),
    ).fetchall()


def planned(connection: Connection, task: TaskRow, session_id: UUID, now: datetime) -> Attempt | None:
    """The next attempt after the device refused `session_id` of the locked task, None when the refusal ends it
    (module docstring). Runs before the refusal is stored."""
    if task.state == "closed":
        return None
    newest = _newest(connection, task.id, settings.capacity_refusals())
    if str(newest[0][0]) != str(session_id):
        return None
    held = cast(
        tuple[bool],
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM hub.audit WHERE session_id = %s AND action = 'session.control'"
            " AND detail->>'action' = 'stop')",
            (session_id,),
        ).fetchone(),
    )[0]
    if held:
        return None
    refused = prompts.routing(connection, [task.id])[str(session_id)]
    if not refused.pending:
        return None
    created: datetime = newest[0][1]
    at = max(now, refused.pending[-1].received_at + prompts.TICK, created + prompts.TICK)
    return Attempt(created=at, triggered_by=prompts.first_author(refused.pending))


def start(connection: Connection, task: TaskRow, attempt: Attempt, effects: Effects, reason: str = REASON) -> TaskRow:
    """Create and place the next attempt (after the refusal is stored, so the scheduler sees it); the derived task.
    At the refusal limit the attempt waits and is held back (module docstring). `reason` is the reason of the
    attempt's first transition: `capacity_exhausted`, or `model_access_unavailable` for the attempt after the owner
    connected model access (`agent_hub.sessions.model_retry`, ADR-079), which is never held back. The attempt is
    triggered by the author of the first prompt it takes over (`triggered_by`, ADR-083 section 2)."""
    created = attempt.created
    limit = settings.capacity_refusals()
    # the attempt counts as the next one in the row: the refusals before it are the row of the refused session
    held = reason == REASON and refusals(_newest(connection, task.id, limit)) >= limit
    session_id = UUID(uuid7(int(created.timestamp() * 1000)))
    if held:
        LOG.warning("capacity_refusal_limit task_id=%s session_id=%s", task.id, session_id)
        decision: Decision = HELD
    else:
        decision = load.decide(
            connection,
            task.wg_id,
            created,
            binding.online_window(),
            pin=load.task_pin(connection, task.id),
            owner=task.owner_id,
        )
    found = create_main_session(
        connection,
        task,
        session_id,
        decision,
        created,
        actor=HUB,
        effects=effects,
        reason=reason,
        triggered_by=attempt.triggered_by,
    )
    if held:
        connection.execute(
            "UPDATE hub.session SET not_before = %s WHERE id = %s",
            (created + timedelta(seconds=settings.refusal_hold_seconds()), session_id),
        )
    return found
