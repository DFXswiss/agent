"""Routes of working modes (ADR-098 sections 1 and 3; 03-api.md sections 4.15 and 6.1).

| Route | Right |
|---|---|
| `GET /v1/modes` | `user`: the catalogue this hub offers (`catalog.offered`) |
| `PUT /v1/tasks/{task}/modes/{mode}` | `task:write`: switch on (ways `web`, `tool`, `inherited`; `modes.choice`) |
| `DELETE /v1/tasks/{task}/modes/{mode}` | `task:write`: switch off |
| `PUT /v1/device/tasks/{task}/modes/{mode}` | `device:wg`: the outcome of an executing `task.mode` (way `sentence`) |
| `PUT /v1/device/tasks/{task}/modes/{mode}/driver` | `device:wg`: the driver state of the mode |

An unknown mode (or a test mode on a hub without `AGENT_HUB_MODES_TEST`) is `404 not_found`; a mode whose needs are
off is `409 mode_needs`; a session's agent that does not pass on a mode of its own task is `403
mode_needs_instruction` (`modes.choice`).

**Switching off** is open to every writer of the workgroup and every agent of one (a session agent included: off
fails safe), reason `person` (`session` for a session agent).

**The device route** accepts the outcome only for an operation `task.mode` of this task that the calling device
registered and that is `accepted` or `executing` (the `executing` transition travels through the outbox and may
arrive later; `404 not_found` otherwise). Switching on needs the operation's `triggered_by` (the verified author of
the instruction the session quoted, checked on the device) to hold `wg:write` now (`403 forbidden`); the chooser is
that person. The quote never reaches the hub (it stays sealed in `operation.params`).

**The driver route** keeps the reported state while the mode is on and answers whether it is on (a driver stops at
its next step boundary when it is not).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from pydantic import StrictBool

from agent_hub.auth.fields import Empty, Strict, Uuid7
from agent_hub.device.access import device_view
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.modes import catalog, store
from agent_hub.modes.choice import announce, choose, offered_mode, session_of_agent
from agent_hub.modes.store import Choice
from agent_hub.orgs import rights
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, TaskRow, lock_task
from agent_hub.tasks.service import task_for_write
from agent_hub.workgroups.routes import path_id

router = HubRouter()
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
MODE_TYPE = "task.mode"
OPEN_OPERATION = ("accepted", "executing")


class DeviceModeBody(Strict):
    """Body of `PUT /v1/device/tasks/{task}/modes/{mode}`."""

    on: StrictBool
    operation_id: Uuid7


class DriverState(Strict):
    step: str
    counters: dict[str, int]
    next_at: str | None
    at: str


class DriverBody(Strict):
    """Body of `PUT /v1/device/tasks/{task}/modes/{mode}/driver` (the contract checks the formats)."""

    driver: DriverState


def mode_name(text: str) -> str:
    """A mode name of a path (`404 not_found` otherwise)."""
    if NAME_RE.fullmatch(text) is None:
        raise HubError("not_found")
    return text


async def _in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            return work(connection)

    return await asyncio.to_thread(run)


@router.get("/v1/modes", auth="browser", right="user", read_resource="modes")
async def list_modes(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return {"modes": catalog.offered(context.config.modes_test)}


@router.put("/v1/tasks/{task}/modes/{mode}", auth="browser", right="wg:write")
async def put_task_mode(
    context: Context, principal: CurrentPrincipal, task: str, mode: str, _body: Empty
) -> dict[str, Any]:
    user, task_id, name = cast(UUID, principal.principal_id), path_id(task), mode_name(mode)

    def work(connection: Any) -> tuple[Effects, dict[str, Any]]:
        row = task_for_write(connection, user, task_id)
        effects = Effects()
        choose(context, connection, principal=principal, user=user, task=row, names=[name], effects=effects)
        found = [item for item in store.active(connection, task_id) if item["mode"] == name]
        return effects, found[0]

    effects, record = await _in_transaction(context, work)
    effects.flush()
    return {"mode": record}


@router.delete("/v1/tasks/{task}/modes/{mode}", auth="browser", right="wg:write")
async def delete_task_mode(context: Context, principal: CurrentPrincipal, task: str, mode: str) -> dict[str, Any]:
    user, task_id, name = cast(UUID, principal.principal_id), path_id(task), mode_name(mode)

    def work(connection: Any) -> Effects:
        row = task_for_write(connection, user, task_id)
        reason = store.SESSION if session_of_agent(connection, principal) is not None else store.PERSON
        effects = Effects()
        if store.switch_off(connection, row, name, actor=Actor("user", user), now=context.clock(), reason=reason):
            announce(connection, effects, row)
        return effects

    (await _in_transaction(context, work)).flush()
    return {}


def _device_task(connection: Any, context: HubContext, device: UUID, task_id: UUID) -> TaskRow:
    row = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task_id,)).fetchone()
    if row is None or not device_view(connection, device, context.config.platform_root_key).reads_wg(row[0]):
        raise HubError("not_found")
    return cast(TaskRow, lock_task(connection, task_id))


def sentence(
    context: HubContext, connection: Any, *, device: UUID, task_id: UUID, name: str, body: DeviceModeBody
) -> tuple[Effects, dict[str, Any] | None]:
    """`PUT /v1/device/tasks/{task}/modes/{mode}` in the caller's transaction (module docstring)."""
    task = _device_task(connection, context, device, task_id)
    operation = UUID(body.operation_id)
    found = connection.execute(
        "SELECT task_id, device_id, type, state, triggered_by FROM hub.operation WHERE id = %s", (operation,)
    ).fetchone()
    if (
        found is None
        or (found[0], found[1], found[2]) != (task_id, device, MODE_TYPE)
        or found[3] not in OPEN_OPERATION
    ):
        raise HubError("not_found")
    effects, now, actor = Effects(), context.clock(), Actor("device", device)
    record: dict[str, Any] | None = None
    if body.on:
        chooser = found[4]
        if chooser is None or not rights.can(connection, chooser, rights.WG_WRITE, wg_id=task.wg_id):
            raise HubError("forbidden")
        mode = offered_mode(context, name)
        choice = Choice(chosen_by=chooser, way=store.SENTENCE, operation_id=operation)
        record, changed = store.switch_on(connection, task, mode, choice, actor=actor, now=now)
    else:
        changed = store.switch_off(
            connection, task, name, actor=actor, now=now, reason=store.SESSION, operation_id=operation
        )
    if changed:
        announce(connection, effects, task)
    return effects, record


@router.put("/v1/device/tasks/{task}/modes/{mode}", auth="device", right="device:wg")
async def device_task_mode(
    context: Context, principal: ActiveDevice, task: str, mode: str, body: DeviceModeBody
) -> dict[str, Any]:
    device, task_id, name = device_id(principal), path_id(task), mode_name(mode)
    effects, record = await _in_transaction(
        context, lambda c: sentence(context, c, device=device, task_id=task_id, name=name, body=body)
    )
    effects.flush()
    return {"mode": record}


@router.put("/v1/device/tasks/{task}/modes/{mode}/driver", auth="device", right="device:wg")
async def device_task_mode_driver(
    context: Context, principal: ActiveDevice, task: str, mode: str, body: DriverBody
) -> dict[str, Any]:
    device, task_id, name = device_id(principal), path_id(task), mode_name(mode)

    def work(connection: Any) -> bool:
        row = _device_task(connection, context, device, task_id)
        state = body.driver.model_dump()
        return store.store_driver(connection, row, name, state, device=device, now=context.clock())

    return {"on": await _in_transaction(context, work)}
