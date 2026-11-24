"""Routes of the placement area (03-api.md sections 4.5, 4.6; ADR-060 section 6): `GET`/`PUT
/v1/workgroups/{wg}/placement` (`wg:read` / `wg:write`) and `PUT /v1/tasks/{task}/placement` (`task:write`).

A changed task choice re-runs the scheduler for the task's workgroup after the commit, so a waiting session follows
it at once ("Choose automatically" while the chosen device is offline or full).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.placement import service
from agent_hub.scheduler import reschedule, settings
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.sessions import rows
from agent_hub.tasks.routes import VM_ID, body_id
from agent_hub.tasks.service import task_for_write
from agent_hub.workgroups.routes import path_id

router = HubRouter()


class PlacementRequest(BaseModel):
    """Body of the `PUT …/placement` routes: a device (None = automatic) and optionally one of its VMs."""

    model_config = ConfigDict(extra="forbid")
    device_id: str | None
    vm_id: str | None


def parsed(body: PlacementRequest) -> tuple[UUID | None, str | None]:
    """The device and VM of a request (`400 request_invalid` with the pointer for a malformed one)."""
    device = None if body.device_id is None else body_id(body.device_id, "/device_id")
    if body.vm_id is not None and VM_ID.fullmatch(body.vm_id) is None:
        raise HubError("request_invalid", detail={"pointers": ["/vm_id"]})
    return device, body.vm_id


def _get(context: HubContext, user: UUID, wg: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        return {"placement": service.json(service.default(connection, wg))}


def _put(context: HubContext, user: UUID, wg: UUID, device: UUID | None, vm: str | None) -> dict[str, Any]:
    with context.pool.connection() as connection, connection.transaction():
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg, write=True)
        pin = service.set_default(connection, user, wg, device=device, vm=vm, now=context.clock())
    return {"placement": service.json(pin)}


def _put_task(context: HubContext, user: UUID, task_id: UUID, device: UUID | None, vm: str | None) -> dict[str, Any]:
    scheduler_signals.bind(context)
    settings.bind(context.config)
    with context.pool.connection() as connection, connection.transaction():
        task = task_for_write(connection, user, task_id)
        service.set_task(
            connection, user, (task.id, task.org_id, task.wg_id), device=device, vm=vm, now=context.clock()
        )
        wg = task.wg_id
    reschedule.reschedule(context, [wg])
    with context.pool.connection() as connection:
        return {"task": rows.task_json(connection, task_id)}


@router.get("/v1/workgroups/{wg}/placement", auth="browser", right="wg:read", read_resource="workgroup.placement")
async def get_placement(context: Context, principal: CurrentPrincipal, wg: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_get, context, user, path_id(wg))


@router.put("/v1/workgroups/{wg}/placement", auth="browser", right="wg:write")
async def put_placement(
    context: Context, principal: CurrentPrincipal, wg: str, body: PlacementRequest
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    device, vm = parsed(body)
    return await asyncio.to_thread(_put, context, user, path_id(wg), device, vm)


@router.put("/v1/tasks/{task}/placement", auth="browser", right="task:write")
async def put_task_placement(
    context: Context, principal: CurrentPrincipal, task: str, body: PlacementRequest
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    device, vm = parsed(body)
    return await asyncio.to_thread(_put_task, context, user, path_id(task), device, vm)
