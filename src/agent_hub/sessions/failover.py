"""The successor of a main session lost with its device (ADR-087 section 3).

Two writers create it: the job `failover.sweep` (`agent_hub.failover.sweep`) when it moves a task off a failed device,
and the transitions (`agent_hub.sessions.transitions.apply`) when a device that comes back after a restart reports an
interrupted main session `failed` with `device_shutdown` or `guest_unreachable` (`REBOOT_REASONS`) - with one device,
the case of operation today: the task then continues on any device, the same one included.

`successor(connection, task, lost, ...)` runs in the caller's transaction after the lost session has ended (the caller
holds the task and the session) and decides:

- **Held:** a user stopped the lost session (audit `session.control` `stop`, like `prompts.carry_over`) or it was
  `stopping` (a stop or a closing task the device never learned): no successor; its undelivered prompts wait for the
  next follow-up, as after any stop.
- **What it takes:** the lost session's undelivered prompts in event order and, when its turn was running (`working`)
  and no user interrupted that turn after it began (audit `session.control` `interrupt` at or after the last
  `working`), first its last accepted prompt again - stored once as `redeliver_event_id` of the lost session, which
  the routing hands on (`agent_hub.sessions.prompts`). Nothing to take and no turn running (`idle`, `suspended`, an
  interrupted turn): no successor.
- **The successor:** a main session with the first transition `requested` and the reason `device_failed`,
  `failover_of` = the lost session, `restore_object` = the newest complete checkpoint of the task now (pinned: a late
  upload of the lost device never changes what the successor restores, `agent_hub.sessions.device_state`),
  `triggered_by` = the author of the first prompt it takes, else the lost session's; placed by `decision`, created
  after every prompt it takes (one tick later, like the hub's other attempts).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.contexts import store as contexts
from agent_hub.crypto.ids import uuid7
from agent_hub.scheduler.core import Decision
from agent_hub.sessions import prompts, rows
from agent_hub.sessions.derive import DEVICE_FAILED, RESUMING, STARTUP, STOPPING, WORKING
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.lifecycle import create_main_session
from agent_hub.sessions.taskstate import HUB, TaskRow

Connection = psycopg.Connection[Any]
REBOOT_REASONS = frozenset({"device_shutdown", "guest_unreachable"})
"""Failure reasons a restarted runner reports for the sessions it lost (05-runner.md section 6)."""
TURN = frozenset({*STARTUP, WORKING, RESUMING})
"""States in which the lost session was doing something the successor continues."""


def held(connection: Connection, session_id: UUID) -> tuple[bool, bool]:
    """`(stopped, turn_interrupted)` by a user (module docstring)."""
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM hub.audit WHERE session_id = %s AND action = 'session.control'"
        " AND detail->>'action' = 'stop'), EXISTS (SELECT 1 FROM hub.audit a WHERE a.session_id = %s"
        " AND a.action = 'session.control' AND a.detail->>'action' = 'interrupt' AND a.at >= COALESCE("
        "(SELECT max(t.received_at) FROM hub.session_transition t WHERE t.session_id = %s AND t.state = 'working'),"
        " '-infinity'::timestamptz))",
        (session_id, session_id, session_id),
    ).fetchone()
    found = cast(tuple[bool, bool], row)
    return bool(found[0]), bool(found[1])


def interrupted_main(connection: Connection, task: TaskRow, session_id: UUID) -> tuple[bool, UUID | None]:
    """`(lost, triggered_by)`: the session is the newest main session of the open task and was interrupted because its
    device failed (the condition of the restart path, module docstring)."""
    row = cast(
        tuple[bool, UUID | None],
        connection.execute(
            "SELECT s.interrupted_at IS NOT NULL AND s.depth = 0 AND s.id = (SELECT n.id FROM hub.session n"
            " WHERE n.task_id = s.task_id AND n.depth = 0 ORDER BY n.created_at DESC, n.id DESC LIMIT 1),"
            " s.triggered_by FROM hub.session s WHERE s.id = %s",
            (session_id,),
        ).fetchone(),
    )
    return bool(row[0]) and task.state != "closed", row[1]


def successor(
    connection: Connection,
    task: TaskRow,
    lost: UUID,
    *,
    was: str,
    triggered_by: UUID | None,
    decision: Decision,
    now: datetime,
    effects: Effects,
) -> UUID | None:
    """Create the successor of the ended main session `lost`, which was in state `was` (module docstring); its ID,
    None when none is due."""
    stopped, interrupted = held(connection, lost)
    if stopped or was == STOPPING:
        return None
    routed = prompts.routing(connection, [task.id])[str(lost)]
    again = routed.delivered[-1:] if was == WORKING and not interrupted else ()
    takes = (*again, *routed.pending)
    if not takes and (was not in TURN or interrupted):
        return None
    if again:
        connection.execute("UPDATE hub.session SET redeliver_event_id = %s WHERE id = %s", (again[0].event_id, lost))
    pinned = contexts.newest(connection, str(task.id))
    created = max(now + prompts.TICK, *(prompt.received_at + prompts.TICK for prompt in takes))
    session_id = UUID(uuid7(int(created.timestamp() * 1000)))
    create_main_session(
        connection,
        task,
        session_id,
        decision,
        created,
        actor=HUB,
        effects=effects,
        reason=DEVICE_FAILED,
        triggered_by=prompts.first_author(takes) if takes else triggered_by,
    )
    connection.execute(
        "UPDATE hub.session SET failover_of = %s, restore_object = %s WHERE id = %s",
        (lost, None if pinned is None else pinned["object_id"], session_id),
    )
    effects.org_frame(task.org_id, "session", rows.session_frame(connection, session_id))
    return session_id
