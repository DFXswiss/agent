"""Who switches a working mode on through a person route, and how (ADR-098 section 3; `agent_hub.modes.routes`).

`person_choice`: a browser session chooses for its user (`web`); an agent of a person outside the platform for its
user (`tool`). An agent whose pairing belongs to a session (`hub.agent.session_id`, the main orchestrator on platform
hardware, ADR-082 section 6) is a session: `403 mode_needs_instruction` - unless it passes on a mode that its own
task has, whose catalogue entry has `passes_on`, to a task it created itself (the task's first `task.meta` event is
the agent's); that is recorded `inherited` with the originating task and the chooser of the originating record.

`choose`: switch on the modes a person route names (task creation with `modes`, `PUT /v1/tasks/{task}/modes/{mode}`)
in the caller's transaction; modes named together satisfy each other's needs. `announce`: the `task` frame and
`reconcile {}` to the devices of the task's live sessions after a change.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, cast
from uuid import UUID

from agent_hub.auth.keys import Principal
from agent_hub.errors import HubError
from agent_hub.http.context import HubContext
from agent_hub.modes import catalog, store
from agent_hub.modes.catalog import Mode
from agent_hub.modes.store import Choice
from agent_hub.sessions import rows
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, TaskRow


def offered_mode(context: HubContext, name: str) -> Mode:
    """The offered catalogue entry of that name (`404 not_found` otherwise)."""
    found = catalog.find(name, context.config.modes_test)
    if found is None:
        raise HubError("not_found")
    return found


def session_of_agent(connection: Any, principal: Principal) -> UUID | None:
    """The session an agent's pairing belongs to (None: a person's agent outside the platform, or no agent)."""
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    if not isinstance(agent, UUID):
        return None
    row = connection.execute("SELECT session_id FROM hub.agent WHERE id = %s", (agent,)).fetchone()
    return None if row is None or row[0] is None else cast(UUID, row[0])


def _created_by(connection: Any, task_id: UUID, agent: object) -> bool:
    row = connection.execute(
        "SELECT author_id FROM hub.event WHERE task_id = %s AND kind = 'task.meta' ORDER BY ts, scope_seq LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None and row[0] == agent


def person_choice(connection: Any, principal: Principal, task_id: UUID, mode: Mode, user: UUID) -> tuple[Choice, Actor]:
    """Who switches `mode` on for the task, and how (module docstring)."""
    if principal.kind != "agent":
        return Choice(chosen_by=user, way=store.WEB), Actor("user", user)
    session = session_of_agent(connection, principal)
    if session is None:
        return Choice(chosen_by=user, way=store.TOOL), Actor("user", user)
    origin = connection.execute("SELECT task_id FROM hub.session WHERE id = %s", (session,)).fetchone()
    record = None
    if mode.passes_on and origin is not None:
        record = connection.execute(
            "SELECT chosen_by FROM hub.task_mode WHERE task_id = %s AND mode = %s AND state = 'on'",
            (origin[0], mode.name),
        ).fetchone()
    if record is None or not _created_by(connection, task_id, principal.attributes.get("agent_id")):
        raise HubError("mode_needs_instruction")
    choice = Choice(chosen_by=cast(UUID, record[0]), way=store.INHERITED, origin_task=cast(UUID, origin[0]))
    return choice, Actor("user", user)


def announce(connection: Any, effects: Effects, task: TaskRow) -> None:
    """The `task` frame and `reconcile {}` to the task's devices (module docstring)."""
    effects.org_frame(task.org_id, "task", cast(dict[str, Any], rows.task_json(connection, task.id)))
    for device in store.devices_of(connection, task.id):
        effects.hint(device, "reconcile", {})


def choose(
    context: HubContext,
    connection: Any,
    *,
    principal: Principal,
    user: UUID,
    task: TaskRow,
    names: Collection[str],
    effects: Effects,
) -> None:
    """Switch on the modes a person route names for a task (module docstring)."""
    now = context.clock()
    modes = [offered_mode(context, name) for name in names]
    chosen = {mode.name for mode in modes}
    changed = False
    for mode in modes:
        choice, actor = person_choice(connection, principal, task.id, mode, user)
        _record, did = store.switch_on(connection, task, mode, choice, actor=actor, now=now, also=chosen)
        changed = changed or did
    if changed:
        announce(connection, effects, task)
