"""Writing tasks: creation, follow-up prompts and closing (03-api.md sections 4.6, 4.6.1, 4.6.3; ADR-002).

**`create_task`** (`POST /v1/tasks`, `wg:write`): the body is `{task_id, wg_id, events:[task.meta, prompt]}`; both
events must be wg-scope events of that workgroup that name the task (`400 request_invalid` with the pointer
otherwise). One transaction (03-api.md section 4.6.1 step 3): the events through `append_events`, the task
(`scheduling`, first `task_transition`), the main session (`requested`, depth 0, `claude-code`), the scheduler's
decision (device and `estimate_ms` and the task `starting`, or the task stays `scheduling` with `device_offline` /
`capacity_exhausted`), the audit row `task.create`. A workgroup without any active assigned device refuses the task
with `409 no_device` and `detail.candidates_available` (platform devices the browser could assign), storing nothing.
After the commit: the event announcement, the hint `session.start`, the `task`/`session` frames and `task.state`.
Idempotency over `task_id`: the same request again (same workgroup, owner and byte-identical events) answers the
current state of the task with `replayed: true`; anything else under a known `task_id` is `409 id_conflict`.

**`add_prompt`** (`POST /v1/tasks/{task}/prompts`, `task:write`): the `prompt` event (of the task, in its workgroup)
is stored, then the task is locked. Follow-ups of one task are serialised before anything else (a per-task advisory
lock, then the hub time, always later than the newest main session), so of two concurrent follow-ups on an ended
session both reach the new main session (A-09). The prompt goes to the main session: a live session gets the hint
`prompt {session_id, scope_id, scope_seq}` from its device (idle: delivered; suspended: resumed first; still starting or
working: queued by the runner); after `stopping`/`stopped`/`failed` (or on a closed task, which is reopened) the hub
creates a new main session placed by the scheduler (`resume_from` is derived in the device state when it lands on the
same device). Replays answer the original `scope_seq` and session.

**`close_task`** (`POST /v1/tasks/{task}/close`, `task:write`): the task becomes `closed` (audit `task.close`), every
live session `stopping` (the device gets `session.control {action:"stop"}`), a session that never got a device
`stopped`. Closing a closed task answers it unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.ids import uuid7
from agent_hub.errors import HubError
from agent_hub.events import notify
from agent_hub.events.store import StoredEvent, append_events, hub_error, stored_event
from agent_hub.http.context import HubContext
from agent_hub.log.events import check_replay
from agent_hub.orgs import rights
from agent_hub.scheduler import load
from agent_hub.sessions import prompts, rows
from agent_hub.sessions.derive import ENDING, FINAL, STOPPED, STOPPING
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.lifecycle import create_main_session, no_device
from agent_hub.sessions.taskstate import Actor, TaskRow, audit, close_task, lock_task, refresh_task
from agent_hub.sessions.transitions import SessionRow, hub_transition, lock_session
from agent_hub.workgroups import read as workgroups

Connection = psycopg.Connection[Any]
KINDS = ("task.meta", "prompt")
CONTROL_HINT = "session.control"
PROMPT_HINT = "prompt"
# The first key of the two-key advisory lock (its key space never meets the one-key locks such as the audit lock);
# the second is a hash of the task ID (a collision only serialises two tasks' follow-ups).
PROMPT_LOCK = 742180201
TICK = timedelta(microseconds=1)


@dataclass
class Outcome:
    """The answer of a write and what to announce after its commit."""

    body: dict[str, Any]
    stored: list[StoredEvent] = field(default_factory=list)
    effects: Effects = field(default_factory=Effects)

    def announce(self) -> None:
        """Send the event announcement and the effects (after the commit)."""
        notify.announce(self.stored)
        self.effects.flush()


def check_event(event: object, index: str, kind: str, task_id: UUID, wg_id: UUID) -> None:
    """A task event names the task and lies in the workgroup scope (`request_invalid` with the pointer otherwise)."""
    header = event.get("header") if isinstance(event, dict) else None
    if not isinstance(header, dict):
        raise HubError("request_invalid", detail={"pointers": [f"{index}/header"]})
    expected = {"kind": kind, "task": str(task_id), "scope": "wg", "scope_id": str(wg_id)}
    for name, value in expected.items():
        if header.get(name) != value:
            raise HubError("request_invalid", detail={"pointers": [f"{index}/header/{name}"]})


def new_session_id(now: datetime) -> UUID:
    """A UUIDv7 for a session the hub creates, at the hub clock."""
    return UUID(uuid7(int(now.timestamp() * 1000)))


def _session_answer(connection: Connection, session_id: str) -> dict[str, Any]:
    session = cast(dict[str, Any], rows.session_json(connection, session_id))
    return {key: session[key] for key in ("session_id", "state", "device_id", "estimate_ms")}


def _first_main(connection: Connection, task_id: UUID) -> str:
    row = connection.execute(
        "SELECT id FROM hub.session WHERE task_id = %s AND depth = 0 ORDER BY created_at, id LIMIT 1", (task_id,)
    ).fetchone()
    return str(cast(tuple[Any, ...], row)[0])


def _replayed_create(
    connection: Connection, user: UUID, task_id: UUID, wg_id: UUID, events: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The answer to a repeated `POST /v1/tasks` (module docstring), `409 id_conflict` for other content."""
    found = connection.execute("SELECT wg_id, owner_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
    if found is None or (found[0], found[1]) != (wg_id, user):
        raise HubError("id_conflict")
    results = []
    for event in events:
        stored = stored_event(connection, str(event["header"]["id"]))
        if stored is None or stored[0]["header"].get("task") != str(task_id):
            raise HubError("id_conflict")
        try:
            check_replay(stored[0], event)
        except CryptoError as error:
            raise hub_error(error) from None
        results.append({"id": event["header"]["id"], "scope_id": stored[1], "scope_seq": stored[2], "replayed": True})
    return {
        "task": rows.task_json(connection, task_id),
        "session": _session_answer(connection, _first_main(connection, task_id)),
        "events": results,
        "replayed": True,
    }


def create_task(
    context: HubContext, user: UUID, task_id: UUID, wg_id: UUID, events: Sequence[dict[str, Any]]
) -> Outcome:
    """`POST /v1/tasks` (module docstring)."""
    now = context.clock()
    actor = Actor("user", user)
    with context.pool.connection() as connection, connection.transaction():
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg_id, write=True)
        for position, (event, kind) in enumerate(zip(events, KINDS, strict=True)):
            check_event(event, f"/events/{position}", kind, task_id, wg_id)
        known = connection.execute("SELECT 1 FROM hub.task WHERE id = %s", (task_id,)).fetchone()
        if known is not None:
            return Outcome(body=_replayed_create(connection, user, task_id, wg_id, events))
        decision = load.decide(connection, wg_id, now, context.config.device_online_seconds)
        if decision is None:
            available = workgroups.has_candidates(connection, wg_id, now, context.config.device_online_seconds)
            raise HubError("no_device", detail={"candidates_available": available})
        stored = append_events(connection, list(events), str(user), now=now, root_key=context.config.platform_root_key)
        if any(item.replayed for item in stored):
            if not all(item.replayed for item in stored):
                raise HubError("id_conflict")
            return Outcome(body=_replayed_create(connection, user, task_id, wg_id, events))
        org_id = cast(UUID, rights.wg_org(connection, wg_id))
        inserted = connection.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
            " VALUES (%s, %s, %s, %s, 'scheduling', %s, %s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
            (task_id, org_id, wg_id, user, now, now, now),
        ).fetchone()
        if inserted is None:  # a concurrent create of the same task_id with other events committed first
            raise HubError("id_conflict")
        connection.execute(
            "INSERT INTO hub.task_transition (task_id, seq, state, reason, at, by_id)"
            " VALUES (%s, 1, 'scheduling', NULL, %s, %s)",
            (task_id, now, user),
        )
        task = TaskRow(task_id, org_id, wg_id, user, "scheduling", None)
        audit(connection, actor, "task.create", task, now, detail={"origin": "browser"})
        outcome = Outcome(body={}, stored=stored)
        session_id = new_session_id(now)
        create_main_session(connection, task, session_id, decision, now, actor=actor, effects=outcome.effects)
        outcome.body = {
            "task": rows.task_json(connection, task_id),
            "session": _session_answer(connection, str(session_id)),
            "events": [item.result() for item in stored],
        }
    return outcome


def _main(connection: Connection, task_id: UUID) -> SessionRow | None:
    row = connection.execute(
        "SELECT id, task_id, device_id, state FROM hub.session WHERE task_id = %s AND depth = 0"
        " ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
        (task_id,),
    ).fetchone()
    return None if row is None else SessionRow(*row)


def task_for_write(connection: Connection, user: UUID, task_id: UUID) -> TaskRow:
    """The task if the user may write it (`404 not_found` unknown, `403 forbidden` without `task:write`)."""
    found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
    if found is None:
        raise HubError("not_found")
    rights.require(connection, user, rights.TASK_WRITE, wg_id=found[0], write=True)
    return cast(TaskRow, lock_task(connection, task_id))


def _prompt_time(connection: Connection, context: HubContext, task_id: UUID) -> datetime:
    """Serialise the follow-up prompts of one task and give the receipt time (A-09).

    A transaction-level advisory lock per task is taken before any other lock (only `add_prompt` takes it, so it
    cannot invert the lock order of the tasks area), and the hub time is read only after it, always later than the
    newest main session (by one tick of PostgreSQL's microsecond resolution at least, so the order of the sessions
    never depends on their IDs). A prompt is thus never received before a main session that a concurrent follow-up
    created first, and it belongs to the session it was routed to (`agent_hub.sessions.prompts`). The one main session
    created outside this lock - by `prompts.carry_over` when the device ends a session (it holds the task lock, which
    is taken after this one, so it cannot take this lock without inverting the order) - can be created after this
    time was read; the prompt then belongs to the ended session and is carried over to that new one, so it is still
    delivered once and in order (only a replay of the request answers the ended session's ID)."""
    connection.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (PROMPT_LOCK, str(task_id)))
    newest = cast(
        tuple[datetime],
        connection.execute(
            "SELECT max(created_at) FROM hub.session WHERE task_id = %s AND depth = 0", (task_id,)
        ).fetchone(),
    )[0]
    return max(context.clock(), newest + TICK)


def add_prompt(context: HubContext, user: UUID, task_id: UUID, event: dict[str, Any]) -> Outcome:
    """`POST /v1/tasks/{task}/prompts` (module docstring)."""
    actor = Actor("user", user)
    with context.pool.connection() as connection, connection.transaction():
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_WRITE, wg_id=found[0], write=True)
        check_event(event, "/event", "prompt", task_id, found[0])
        now = _prompt_time(connection, context, task_id)
        stored = append_events(connection, [event], str(user), now=now, root_key=context.config.platform_root_key)
        item = stored[0]
        received = cast(
            tuple[datetime],
            connection.execute("SELECT received_at FROM hub.event WHERE id = %s", (item.id,)).fetchone(),
        )[0]
        if item.replayed:
            session_id = prompts.session_of(connection, task_id, received)
            return Outcome(body={"scope_seq": item.scope_seq, "session_id": session_id, "replayed": True})
        task = cast(TaskRow, lock_task(connection, task_id))
        outcome = Outcome(body={}, stored=stored)
        main = _main(connection, task_id)
        if main is None or main.state in ENDING or task.state == "closed":
            decision = no_device(load.decide(connection, task.wg_id, now, context.config.device_online_seconds))
            new_id = new_session_id(now)
            create_main_session(
                connection,
                task,
                new_id,
                decision,
                now,
                actor=actor,
                effects=outcome.effects,
                reopen=task.state == "closed",
            )
            session_id = str(new_id)
        else:
            session_id = str(main.id)
            if main.device_id is not None:
                outcome.effects.hint(
                    main.device_id,
                    PROMPT_HINT,
                    {"session_id": session_id, "scope_id": item.scope_id, "scope_seq": item.scope_seq},
                )
            refresh_task(connection, task, now, actor, outcome.effects)
        outcome.body = {"scope_seq": item.scope_seq, "session_id": session_id}
    return outcome


def stop_sessions(connection: Connection, task: TaskRow, now: datetime, actor: Actor, effects: Effects) -> None:
    """Every live session of a locked task: `stopping` with a stop hint, or `stopped` without a device."""
    found = connection.execute(
        "SELECT id, task_id, device_id, state FROM hub.session WHERE task_id = %s ORDER BY id FOR UPDATE", (task.id,)
    ).fetchall()
    for row in found:
        session = SessionRow(*row)
        if session.state in FINAL:
            continue
        if session.device_id is None:
            hub_transition(connection, task, session, STOPPED, now, actor=actor, effects=effects)
        elif hub_transition(connection, task, session, STOPPING, now, actor=actor, effects=effects):
            effects.hint(session.device_id, CONTROL_HINT, {"session_id": str(session.id), "action": "stop"})


def close(context: HubContext, user: UUID, task_id: UUID) -> Outcome:
    """`POST /v1/tasks/{task}/close` (module docstring)."""
    now = context.clock()
    actor = Actor("user", user)
    with context.pool.connection() as connection, connection.transaction():
        task = task_for_write(connection, user, task_id)
        outcome = Outcome(body={})
        if task.state != "closed":
            closed = close_task(connection, task, now, actor, outcome.effects)
            stop_sessions(connection, closed, now, actor, outcome.effects)
            audit(connection, actor, "task.close", closed, now, detail={"by": "user"})
            outcome.effects.org_frame(closed.org_id, "task", rows.task_json(connection, task_id) or {})
        outcome.body = {"task": rows.task_json(connection, task_id)}
    return outcome


def control(context: HubContext, user: UUID, session_id: UUID, action: str) -> Outcome:
    """`POST /v1/sessions/{session}/control` (`task:write`): the hint `session.control {session_id, action}` to the
    session's device and the audit row `session.control`. A session that already ended is left alone; a session
    without a device has no runner to ask: `stop` ends it at once (`stopped`), `interrupt` does nothing."""
    now = context.clock()
    actor = Actor("user", user)
    with context.pool.connection() as connection, connection.transaction():
        found = connection.execute("SELECT task_id FROM hub.session WHERE id = %s", (session_id,)).fetchone()
        if found is None:
            raise HubError("not_found")
        task = task_for_write(connection, user, found[0])
        session = cast(SessionRow, lock_session(connection, session_id))
        outcome = Outcome(body={})
        if session.state not in FINAL:
            if session.device_id is not None:
                outcome.effects.hint(session.device_id, CONTROL_HINT, {"session_id": str(session_id), "action": action})
            elif action == "stop":
                hub_transition(connection, task, session, STOPPED, now, actor=actor, effects=outcome.effects)
        audit(
            connection,
            actor,
            "session.control",
            task,
            now,
            detail={"action": action},
            session_id=session.id,
            device_id=session.device_id,
            target=("session", session.id),
        )
    return outcome
