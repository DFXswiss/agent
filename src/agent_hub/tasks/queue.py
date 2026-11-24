"""The queue of a task's prompts and the withdrawal of a waiting prompt (03-api.md section 4.6, ADR-083 section 3;
KOR-46).

**Queue** (`GET /v1/tasks/{task}/queue`, `task:read`): the prompts routed to a live main session (neither `stopped`
nor `failed`) and not delivered yet, in event order (`agent_hub.sessions.prompts`: a prompt carried over is held by
the session it was carried to). Prompts of a failed session wait for "Retry" and are not in the queue; a withdrawn
prompt has left the routing. Each entry names its author, whether the hub received it from the author's agent
(`via_agent`), the holding session and its device and a state: `withdrawing` while a requested withdrawal is not
confirmed, else `waiting_for_device` when the session has no device or its device is not online
(`agent_hub.stream.presence`), else `waiting`. `withdrawals` lists the task's withdrawals, newest first (at most
`WITHDRAWALS_MAX`): `withdrawn` (confirmed), `withdrawing` (still pending in the routing) or `too_late` (no longer
pending and not withdrawn: it was delivered meanwhile).

**Withdraw** (`POST /v1/tasks/{task}/prompts/{event}/withdraw`, `task:write`): only the prompt's author (a user's
agent acts as its user) may take back a prompt of the queue. One transaction, serialised with the follow-ups of the
task (the per-task prompt lock of `agent_hub.tasks.service`, then the task row):

- `404 not_found` when the event is not a prompt of the task, `403 forbidden` for another person's prompt;
- a repeat answers the current state of the withdrawal (`withdrawn`, or `withdrawing` while it is still pending);
  once it is neither, and for a prompt that is not in the queue (delivered, refused, held by a failed session), the
  answer is `409 version_conflict` (the hub has no `state_conflict` code);
- the holding session has **no device**: the prompt is withdrawn at once (`withdrawn_at`); a main session that never
  delivered a prompt and has nothing left to deliver is stopped as well (`stopped`, as a session without a device
  that its user stops);
- otherwise the device decides, because only it knows whether the guest has the prompt: a device that has not
  announced the feature `prompt.withdraw` is `409 peer_outdated` (`detail.devices`); else the request is stored, the
  device gets the hint `reconcile` and finds the prompt in `prompt_queue.withdrawn` of `GET /v1/device/state`; it
  reports `session.prompt_withdrawn` (`agent_hub.sessions.outbox`) or delivers the prompt (`too_late`).

The answer is `{state}`; the audit row `prompt.withdraw {state}` names the author and the prompt event, and the
`session` frame of the holding session tells the browsers to read the queue again.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.recorder import unchanged
from agent_hub.crypto.encoding import format_time
from agent_hub.device.features import PROMPT_WITHDRAW, outdated_devices
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from agent_hub.orgs import rights
from agent_hub.sessions import prompts, rows
from agent_hub.sessions.derive import STOPPED
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, TaskRow, audit, lock_task
from agent_hub.sessions.transitions import SessionRow, hub_transition, lock_session
from agent_hub.stream import presence
from agent_hub.tasks.service import PROMPT_LOCK, Outcome

Connection = psycopg.Connection[Any]
WITHDRAWALS_MAX = 100
RECONCILE_HINT = "reconcile"
CONFLICT = "version_conflict"


def live_mains(connection: Connection, task_id: UUID | str) -> dict[str, UUID | None]:
    """The device (or None) of every live main session of the task (neither `stopped` nor `failed`), by ID."""
    found = connection.execute(
        "SELECT id, device_id FROM hub.session WHERE task_id = %s AND depth = 0 AND state NOT IN ('stopped', 'failed')",
        (task_id,),
    ).fetchall()
    return {str(row[0]): row[1] for row in found}


def queued(
    connection: Connection, task_id: UUID | str
) -> tuple[dict[str, tuple[prompts.Prompt, prompts.Routed]], list[tuple[prompts.Prompt, str, UUID | None]]]:
    """Every pending prompt of the task with its holder (`agent_hub.sessions.prompts.holders`) and the queue: the
    prompts held by a live main session with that session and its device, in event order."""
    held = prompts.holders(prompts.routing(connection, [task_id]))
    live = live_mains(connection, task_id)
    queue = [
        (prompt, item.session_id, live[item.session_id]) for prompt, item in held.values() if item.session_id in live
    ]
    return held, sorted(queue, key=lambda entry: entry[0].scope_seq)


def queue_json(connection: Connection, task_id: UUID, now: datetime, window: int) -> dict[str, Any]:
    """The answer of `GET /v1/tasks/{task}/queue` (module docstring)."""
    held, queue = queued(connection, task_id)
    ids = [prompt.event_id for prompt, _session, _device in queue]
    agent, withdrawing = prompts.via_agent(connection, ids), prompts.withdrawing(connection, ids)
    online = presence.online_devices(
        connection, [device for _p, _s, device in queue if device is not None], now, window
    )
    entries = []
    for prompt, session, device in queue:
        if prompt.event_id in withdrawing:
            state = "withdrawing"
        elif device is None or device not in online:
            state = "waiting_for_device"
        else:
            state = "waiting"
        entries.append(
            {
                "event_id": prompt.event_id,
                "author_id": prompt.author_id,
                "via_agent": prompt.event_id in agent,
                "scope_seq": prompt.scope_seq,
                "received_at": format_time(prompt.received_at),
                "session_id": session,
                "device_id": None if device is None else str(device),
                "state": state,
            }
        )
    withdrawals = connection.execute(
        "SELECT event_id, requested_at, withdrawn_at FROM hub.prompt_withdrawal WHERE task_id = %s"
        " ORDER BY requested_at DESC, event_id DESC LIMIT %s",
        (task_id, WITHDRAWALS_MAX),
    ).fetchall()
    return {
        "task_id": str(task_id),
        "prompts": entries,
        "withdrawals": [
            {
                "event_id": str(event),
                "state": "withdrawn" if withdrawn is not None else "withdrawing" if str(event) in held else "too_late",
                "requested_at": format_time(requested),
            }
            for event, requested, withdrawn in withdrawals
        ],
    }


def read_queue(context: HubContext, user: UUID, task_id: UUID) -> dict[str, Any]:
    """`GET /v1/tasks/{task}/queue`: `404 not_found` for an unknown task or one the user may not read."""
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return queue_json(connection, task_id, context.clock(), context.config.device_online_seconds)


def _repeat(connection: Connection, task_id: UUID, event_id: str, withdrawn_at: datetime | None) -> str:
    """The current state of a withdrawal requested before (module docstring)."""
    if withdrawn_at is None and event_id not in queued(connection, task_id)[0]:
        raise HubError(CONFLICT)
    unchanged()
    return "withdrawing" if withdrawn_at is None else "withdrawn"


def _stop_if_empty(
    connection: Connection, task: TaskRow, session_id: str, now: datetime, *, actor: Actor, effects: Effects
) -> None:
    """Stop a main session without a device that never delivered a prompt and has nothing left to deliver."""
    routed = prompts.routing(connection, [task.id])[session_id]
    if routed.delivered or routed.pending:
        return
    session = cast(SessionRow, lock_session(connection, session_id))
    hub_transition(connection, task, session, STOPPED, now, actor=actor, effects=effects)


def withdraw(context: HubContext, user: UUID, task_id: UUID, event_id: UUID) -> Outcome:
    """`POST /v1/tasks/{task}/prompts/{event}/withdraw` (module docstring)."""
    now = context.clock()
    actor = Actor("user", user)
    with context.pool.connection() as connection, connection.transaction():
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_WRITE, wg_id=found[0], write=True)
        connection.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (PROMPT_LOCK, str(task_id)))
        task = cast(TaskRow, lock_task(connection, task_id))
        author = connection.execute(
            "SELECT author_id FROM hub.event WHERE id = %s AND task_id = %s AND kind = 'prompt'", (event_id, task_id)
        ).fetchone()
        if author is None:
            raise HubError("not_found")
        if author[0] != user:
            raise HubError("forbidden")
        event = str(event_id)
        before = connection.execute(
            "SELECT withdrawn_at FROM hub.prompt_withdrawal WHERE event_id = %s", (event_id,)
        ).fetchone()
        if before is not None:
            return Outcome(body={"state": _repeat(connection, task_id, event, before[0])})
        _held, queue = queued(connection, task_id)
        holder = next(((session, device) for prompt, session, device in queue if prompt.event_id == event), None)
        if holder is None:
            raise HubError(CONFLICT)
        session_id, device = holder
        if device is not None and outdated_devices(connection, [str(device)], PROMPT_WITHDRAW):
            raise HubError("peer_outdated", detail={"devices": [str(device)]})
        state = "withdrawn" if device is None else "withdrawing"
        connection.execute(
            "INSERT INTO hub.prompt_withdrawal (event_id, task_id, requested_by, requested_at, withdrawn_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (event_id, task_id, user, now, now if device is None else None),
        )
        outcome = Outcome(body={"state": state})
        if device is None:
            _stop_if_empty(connection, task, session_id, now, actor=actor, effects=outcome.effects)
        else:
            outcome.effects.hint(device, RECONCILE_HINT, {})
        audit(
            connection,
            actor,
            "prompt.withdraw",
            task,
            now,
            detail={"state": state},
            session_id=UUID(session_id),
            device_id=device,
            target=("event", event_id),
        )
        outcome.effects.org_frame(task.org_id, "session", rows.session_frame(connection, session_id))
    return outcome
