"""Session transitions (02-data-model.md sections 2.4, 2.4.1; 03-api.md section 6.1 `session_transition`).

`apply(connection, session_id, state, reason, at, received_at=..., reported_by=...)` is the public entry for the
outbox (HUB-10): it runs inside the caller's transaction, after the caller's `append_events` and before any of the
caller's audit rows (lock order of `agent_hub.sessions.taskstate`). It

1. finds the session (`404 not_found`) and checks that it runs on the reporting device (`403 forbidden`);
2. checks the reason: `failed` needs a session failure reason, every other state none or an error code
   (`400 request_invalid`);
3. checks the state machine (`agent_hub.sessions.derive.step`): the current state again is no change
   (`Applied.changed` False), a refused step raises `TransitionRefused` (`400 request_invalid`: the outbox
   acknowledges such an item as `rejected`);
4. appends `session_transition` (`at` = device clock, `received_at` = hub clock, `reported_by`), updates the session
   (`state`, `state_reason`, `ready_at` = receive time of the first `prompt_delivering`, `first_working_at` = receive
   time of the first `working`, `ended_at` for `stopped`/`failed`, reported `agent_version`/`model_access`/
   `model_id`), writes the audit row `session.<state>` and derives the task (`refresh_task`);
5. records the `session` frame (row + transition), the `task` frame and the signal `task.state` in `Applied.effects`;
6. after a `stopped`: lets `agent_hub.sessions.prompts.carry_over` start the next main session for the prompts the
   ended one did not deliver (KOR-11); for a `failed` with `capacity_exhausted` (the device had no free VM slot):
   `agent_hub.sessions.retry` decides before step 4 whether the hub re-schedules, and then step 4 stores the refusal
   without deriving the task and the next attempt is created and placed, which derives it (KOR-14); a `failed`
   with `model_access_unavailable` gets the same attempt when a model credential of the owner arrived while the start
   ran (`agent_hub.sessions.model_retry`, ADR-077; otherwise the owner's next credential write creates it).

The caller sends the effects with `Applied.effects.flush()` **after its commit**. `hub_transition` is the same step for
changes the hub makes itself (closing a task, stopping a session without device).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.errors import HubError
from agent_hub.sessions import model_retry, prompts, retry, rows
from agent_hub.sessions.derive import (
    ALLOWED,
    FAILED,
    REFUSED,
    SAME,
    STATES,
    STOPPED,
    step,
)
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, TaskRow, audit, lock_task, refresh_task

Connection = psycopg.Connection[Any]
SESSION_FAILURE_REASONS = frozenset(
    {
        "model_access_unavailable",
        "vm_start_failed",
        "guest_unreachable",
        "sandbox_failed",
        "agent_start_failed",
        "agent_exited",
        "prompt_refused",
        "delivery_failed",
        "device_offline",
        "device_shutdown",
        "capacity_exhausted",
        "runtime_exceeded",
        "budget_exceeded",
        "credit_exhausted",
        "oom",
        "unassigned",
        "hook_unavailable",
    }
)
"""A copy of `contracts/enums/session-failure-reasons.json` (a test compares them)."""
MODEL_ACCESS = frozenset({"subscription", "api_key", "platform"})
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class TransitionRefused(HubError):
    """A transition the state machine does not allow (`400 request_invalid`)."""

    def __init__(self) -> None:
        super().__init__("request_invalid")


@dataclass(frozen=True)
class SessionRow:
    """The fields of a session the transitions need."""

    id: UUID
    task_id: UUID
    device_id: UUID | None
    state: str


@dataclass
class Applied:
    """The outcome of `apply`: whether the state changed and the effects to send after the commit."""

    changed: bool
    state: str
    effects: Effects = field(default_factory=Effects)


def lock_session(connection: Connection, session_id: UUID | str) -> SessionRow | None:
    """The session held `FOR UPDATE` (lock its task first), None for an unknown session."""
    row = connection.execute(
        "SELECT id, task_id, device_id, state FROM hub.session WHERE id = %s FOR UPDATE", (session_id,)
    ).fetchone()
    return None if row is None else SessionRow(*row)


def check_reason(state: str, reason: object) -> str | None:
    """The reason of a reported state (`request_invalid` when it does not fit the state)."""
    if state == FAILED:
        if reason not in SESSION_FAILURE_REASONS:
            raise HubError("request_invalid")
        return str(reason)
    if reason is None:
        return None
    if not isinstance(reason, str) or _ERROR_CODE_RE.fullmatch(reason) is None:
        raise HubError("request_invalid")
    return reason


def record(
    connection: Connection,
    task: TaskRow,
    session: SessionRow,
    state: str,
    reason: str | None,
    *,
    at: datetime,
    received_at: datetime,
    actor: Actor,
    effects: Effects,
    details: dict[str, str] | None = None,
    derive: bool = True,
) -> None:
    """Store one allowed transition of a locked session of a locked task (module docstring, step 4-5); `derive` False
    leaves the task derivation to the caller."""
    reported = details or {}
    connection.execute(
        "INSERT INTO hub.session_transition (session_id, seq, state, reason, at, received_at, reported_by) VALUES"
        " (%s, (SELECT COALESCE(MAX(seq), 0) + 1 FROM hub.session_transition WHERE session_id = %s), %s, %s, %s, %s,"
        " %s)",
        (session.id, session.id, state, reason, at, received_at, actor.id if actor.kind == "device" else None),
    )
    updated = connection.execute(
        "UPDATE hub.session SET state = %s, state_reason = %s,"
        " ready_at = CASE WHEN %s = 'prompt_delivering' THEN COALESCE(ready_at, %s) ELSE ready_at END,"
        " first_working_at = CASE WHEN %s = 'working' THEN COALESCE(first_working_at, %s) ELSE first_working_at END,"
        " ended_at = CASE WHEN %s IN ('stopped', 'failed') THEN %s ELSE ended_at END,"
        " agent_version = COALESCE(%s, agent_version), model_access = COALESCE(%s, model_access),"
        " model_id = COALESCE(%s, model_id) WHERE id = %s RETURNING model_access, model_id",
        (
            state,
            reason,
            state,
            received_at,
            state,
            received_at,
            state,
            received_at,
            reported.get("agent_version"),
            reported.get("model_access"),
            reported.get("model_id"),
            session.id,
        ),
    ).fetchone()
    model_access, model_id = cast(tuple[str | None, str | None], updated)
    detail: dict[str, Any] = {"reason": reason, "model_access": model_access}
    if state != FAILED:
        detail["model_id"] = model_id
    audit(
        connection,
        actor,
        f"session.{state}",
        task,
        received_at,
        detail=detail,
        session_id=session.id,
        device_id=session.device_id,
        target=("session", session.id),
    )
    effects.org_frame(task.org_id, "session", rows.session_frame(connection, session.id))
    if derive:
        refresh_task(connection, task, received_at, actor, effects)


def _details(agent_version: str | None, model_access: str | None, model_id: str | None) -> dict[str, str]:
    if model_access is not None and model_access not in MODEL_ACCESS:
        raise HubError("request_invalid")
    found = {"agent_version": agent_version, "model_access": model_access, "model_id": model_id}
    return {key: value for key, value in found.items() if value is not None}


def apply(
    connection: Connection,
    session_id: UUID | str,
    state: str,
    reason: str | None,
    at: datetime,
    *,
    received_at: datetime,
    reported_by: UUID | str | None,
    agent_version: str | None = None,
    model_access: str | None = None,
    model_id: str | None = None,
) -> Applied:
    """Apply a reported transition in the caller's transaction (module docstring).

    `reported_by` is the reporting device (None only for a hub-internal caller); refusals raise `HubError`.
    """
    if state not in STATES:
        raise HubError("request_invalid")
    checked = check_reason(state, reason)
    found = connection.execute(
        "SELECT s.task_id, t.owner_id FROM hub.session s JOIN hub.task t ON t.id = s.task_id WHERE s.id = %s",
        (session_id,),
    ).fetchone()
    if found is None:
        raise HubError("not_found")
    if state == FAILED and checked == model_retry.REASON:
        model_retry.lock_owner(connection, found[1])  # before the task lock, as the credential write (ADR-077)
    task = cast(TaskRow, lock_task(connection, found[0]))
    session = cast(SessionRow, lock_session(connection, session_id))
    if reported_by is not None and str(session.device_id) != str(reported_by):
        raise HubError("forbidden")
    details = _details(agent_version, model_access, model_id)
    result = step(session.state, state)
    if result == SAME:
        return Applied(changed=False, state=state)
    if result == REFUSED:
        raise TransitionRefused()
    actor = Actor("device", UUID(str(reported_by))) if reported_by is not None else Actor("hub")
    applied = Applied(changed=True, state=state)
    refused = state == FAILED and checked == retry.REASON
    attempt = retry.planned(connection, task, session.id, received_at) if refused else None
    attempt_reason = retry.REASON
    if state == FAILED and checked == model_retry.REASON:
        attempt = model_retry.planned(connection, task, session.id, received_at)
        attempt_reason = model_retry.REASON
    record(
        connection,
        task,
        session,
        state,
        checked,
        at=at,
        received_at=received_at,
        actor=actor,
        effects=applied.effects,
        details=details,
        derive=attempt is None,
    )
    if attempt is not None:
        retry.start(connection, task, attempt, applied.effects, attempt_reason)
    if state == STOPPED:
        prompts.carry_over(
            connection, cast(TaskRow, lock_task(connection, task.id)), session.id, received_at, applied.effects
        )
    return applied


def hub_transition(
    connection: Connection,
    task: TaskRow,
    session: SessionRow,
    state: str,
    now: datetime,
    *,
    actor: Actor,
    effects: Effects,
) -> bool:
    """A transition the hub makes itself on a locked session of a locked task (`stopping` when a task closes,
    `stopped` for a session that never reached a device); False when the session already ended or is there."""
    if step(session.state, state) != ALLOWED:
        return False
    record(connection, task, session, state, None, at=now, received_at=now, actor=actor, effects=effects)
    return True
