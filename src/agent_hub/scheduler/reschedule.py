"""Re-scheduling of waiting sessions (03-api.md section 4.6.2: "the hub re-runs the scheduler when a device of the
workgroup connects or reports capacity"; ADR-075).

A waiting session is a session in state `requested` without a device whose task is not closed and whose
`not_before` (an attempt held back after repeated refusals, ADR-075 section 7) has passed. `reschedule(context,
wg_ids)` loads the scheduler's inputs once per workgroup and runs the scheduler for each of its waiting sessions,
oldest first, with the task's chosen device and VM (`hub.task.placement_*`), booking every placement on the chosen
candidate (a pending session there; a new VM it needs takes a slot and one VM of the workgroup's allowance), so a
device that comes back with one free slot does not get every waiting session: with a device the session is placed
(hint `session.start`, task `starting`); without one the task keeps waiting and gets the current reason; a session
whose reason did not change is left untouched. Each session is handled in its own short transaction under the lock
order of the tasks area (task row, then session row); a session that got a device or ended meanwhile is skipped. The
effects go out after each commit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from agent_hub.db.pool import transaction
from agent_hub.scheduler import load
from agent_hub.scheduler.core import Candidate, NoPlacement, Placement, choose
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.lifecycle import place
from agent_hub.sessions.taskstate import HUB, TaskRow, lock_task
from agent_hub.sessions.transitions import SessionRow, lock_session

_WAITING_FROM = (
    " FROM hub.session s JOIN hub.task t ON t.id = s.task_id WHERE s.device_id IS NULL AND s.state = 'requested'"
    " AND t.state <> 'closed' AND (s.not_before IS NULL OR s.not_before <= %s)"
)
BY_DEVICE = " AND s.wg_id IN (SELECT a.wg_id FROM hub.wg_device a WHERE a.device_id = %s AND a.unassigned_seq IS NULL)"
"""Restriction of `waiting_wgs` to the workgroups of a device."""
BY_ORG = " AND s.org_id = %s"
"""Restriction of `waiting_wgs` to the workgroups of an org."""


def waiting_wgs(connection: Any, now: datetime, where: str = "", params: tuple[Any, ...] = ()) -> list[UUID]:
    """The workgroups with waiting sessions at the hub time `now` (all, or restricted by `BY_DEVICE` / `BY_ORG`)."""
    found = connection.execute("SELECT DISTINCT s.wg_id" + _WAITING_FROM + where + " ORDER BY s.wg_id", (now, *params))
    return [UUID(str(row[0])) for row in found.fetchall()]


def booked(found: list[Candidate], device_id: str) -> list[Candidate]:
    """The candidates after a session of the workgroup was placed on `device_id`: one more pending session there (it
    uses the room of the device's VMs of the workgroup, else a new VM, `agent_hub.scheduler.core`)."""
    return [item if item.device_id != device_id else replace(item, pending=item.pending + 1) for item in found]


def reschedule(context: Any, wg_ids: Iterable[UUID]) -> int:
    """Place the waiting sessions of the workgroups (module docstring); the number of sessions placed."""
    placed = 0
    online_seconds = context.config.device_online_seconds
    for wg in sorted(set(wg_ids)):
        with transaction(context.pool) as connection:
            waiting = connection.execute(
                "SELECT s.id, s.task_id" + _WAITING_FROM + " AND s.wg_id = %s ORDER BY s.created_at, s.id",
                (context.clock(), wg),
            ).fetchall()
            found = load.candidates(connection, wg, context.clock(), online_seconds)
            allowance = load.allowance(connection, wg)
            pins = {str(task_id): load.task_pin(connection, task_id) for _session, task_id in waiting}
        for session_id, task_id in waiting:
            effects = Effects()
            with transaction(context.pool) as connection:
                now = context.clock()
                task = cast(TaskRow, lock_task(connection, task_id))
                session = cast(SessionRow, lock_session(connection, session_id))
                if task.state == "closed" or session.device_id is not None or session.state != "requested":
                    continue
                pick, pinned = load.pinned(load.with_owner(connection, found, task.owner_id), pins[str(task_id)])
                decision = choose(pick, allowance, pinned=pinned) if pick else load.without_device(connection, wg)
                if isinstance(decision, NoPlacement) and decision.reason == task.state_reason:
                    continue
                place(connection, task, session_id, decision, now, actor=HUB, effects=effects)
                if isinstance(decision, Placement):
                    placed += 1
                    found = booked(found, decision.device_id)
            effects.flush()
    return placed
