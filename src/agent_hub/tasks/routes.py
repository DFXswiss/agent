"""Routes of the tasks area (03-api.md section 4.6): `POST /v1/tasks`, `GET /v1/workgroups/{wg}/tasks`,
`GET /v1/tasks/{task}`, `POST /v1/tasks/{task}/prompts` and `POST /v1/tasks/{task}/close`.

The writes live in `agent_hub.tasks.service` (one transaction each, effects sent after the commit), the reads in
`agent_hub.tasks.read`. Reads answer `404 not_found` for unknown, malformed and forbidden IDs alike; writes `403
forbidden` on a known task without `task:write` (03-api.md section 1). There is no project level anywhere (R-2.10):
organization → workgroup → task. The task's GitHub identity (`github_identity` of `POST /v1/tasks`, ADR-078) is
checked by the service; `PUT /v1/tasks/{task}/github-identity` lives in `agent_hub.github.routes`.
"""

from __future__ import annotations

import asyncio
import functools
import re
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.agents.identity import actor
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count, parse_uuid
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.scheduler import settings as scheduler_settings
from agent_hub.scheduler import signals as scheduler_signals
from agent_hub.sessions.derive import TASK_STATES
from agent_hub.tasks import read, service
from agent_hub.workgroups.routes import path_id

router = HubRouter()
VM_ID = re.compile(r"vm-[0-9a-f]{8}")


class Placement(BaseModel):
    """The chosen device and optionally one of its VMs (ADR-075 section 6)."""

    model_config = ConfigDict(extra="forbid")
    device_id: str
    vm_id: str | None = None


class CreateTaskRequest(BaseModel):
    """Body of `POST /v1/tasks`; the events are checked by the service and the crypto layer."""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    wg_id: str
    events: list[dict[str, Any]] = Field(min_length=2, max_length=2)
    placement: Placement | None = None
    github_identity: str | None = None


class PromptRequest(BaseModel):
    """Body of `POST /v1/tasks/{task}/prompts`."""

    model_config = ConfigDict(extra="forbid")
    event: dict[str, Any]


class CloseRequest(BaseModel):
    """Body of `POST /v1/tasks/{task}/close` (empty)."""

    model_config = ConfigDict(extra="forbid")


def body_id(text: str, pointer: str) -> UUID:
    """A body ID in canonical UUID form (`400 request_invalid` with the pointer otherwise)."""
    try:
        return cast(UUID, parse_uuid(text))
    except ValueError:
        raise HubError("request_invalid", detail={"pointers": [pointer]}) from None


def _run(context: HubContext, write: Any, *args: Any) -> dict[str, Any]:
    """Run a write of the service, then announce its effects (after the commit)."""
    scheduler_signals.bind(context)
    scheduler_settings.bind(context.config)
    outcome: service.Outcome = write(context, *args)
    outcome.announce()
    return outcome.body


@router.post("/v1/tasks", auth="browser", right="wg:write")
async def create_task(context: Context, principal: CurrentPrincipal, body: CreateTaskRequest) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    task_id, wg_id = body_id(body.task_id, "/task_id"), body_id(body.wg_id, "/wg_id")
    requested = None
    if body.placement is not None:
        vm = body.placement.vm_id
        if vm is not None and VM_ID.fullmatch(vm) is None:
            raise HubError("request_invalid", detail={"pointers": ["/placement/vm_id"]})
        requested = (body_id(body.placement.device_id, "/placement/device_id"), vm)
    identity = None if body.github_identity is None else body_id(body.github_identity, "/github_identity")
    write = functools.partial(
        service.create_task, requested=requested, github_identity=identity, author=actor(principal).id
    )
    return await asyncio.to_thread(_run, context, write, user, task_id, wg_id, body.events)


def _list(context: HubContext, user: UUID, wg: UUID, **options: Any) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        return read.list_tasks(connection, wg, **options)


@router.get("/v1/workgroups/{wg}/tasks", auth="browser", right="wg:read", read_resource="workgroup.tasks")
async def list_tasks(
    context: Context,
    principal: CurrentPrincipal,
    wg: str,
    *,
    state: str | None = None,
    before: str | None = None,
    limit: str | None = None,
    order: str | None = None,
) -> dict[str, Any]:
    if state is not None and state not in TASK_STATES:
        raise HubError("request_invalid", detail={"pointers": ["/state"]})
    if order is not None and order not in read.ORDERS:
        raise HubError("request_invalid", detail={"pointers": ["/order"]})
    count = parse_count(limit, read.LIST_DEFAULT, 1, read.LIST_MAX)
    user = cast(UUID, principal.principal_id)
    options = {"state": state, "before": before, "limit": count, "order": order or "created"}
    return await asyncio.to_thread(_list, context, user, path_id(wg), **options)


def _task(context: HubContext, user: UUID, task: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return read.get_task(connection, task)


@router.get("/v1/tasks/{task}", auth="browser", right="task:read", read_resource="task")
async def get_task(context: Context, principal: CurrentPrincipal, task: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_task, context, user, path_id(task))


@router.post("/v1/tasks/{task}/prompts", auth="browser", right="task:write")
async def post_prompt(context: Context, principal: CurrentPrincipal, task: str, body: PromptRequest) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    write = functools.partial(service.add_prompt, author=actor(principal).id)
    return await asyncio.to_thread(_run, context, write, user, path_id(task), body.event)


@router.post("/v1/tasks/{task}/close", auth="browser", right="task:write")
async def close_task(context: Context, principal: CurrentPrincipal, task: str, body: CloseRequest) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_run, context, service.close, user, path_id(task))
