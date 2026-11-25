"""Routes of the operations area (03-api.md sections 4.7 and 8.3).

| Route | Right | Module |
|---|---|---|
| `POST /v1/device/operations` | `device:session` | `register` |
| `GET /v1/tasks/{task}/operations?before&limit` | `task:read` | `read` |
| `GET /v1/operations/{id}` | `task:read` | `read` |

The registration runs in one transaction in a worker thread (run once more when PostgreSQL aborts it as a deadlock
victim, like the outbox push); its frames, hints and signals go out after the commit through
`agent_hub.stream.publish` and `agent_hub.signals`. The reads answer `404 not_found` for unknown, malformed and
foreign IDs alike and are read-logged by the pipeline (`read.task.operations`, `read.operation`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.operations import read
from agent_hub.operations.register import Outcome, RegistrationRequest, register
from agent_hub.orgs import rights
from agent_hub.workgroups.routes import path_id

router = HubRouter()


@router.post("/v1/device/operations", auth="device", right="device:session")
async def post_operation(context: Context, principal: ActiveDevice, body: RegistrationRequest) -> dict[str, Any]:
    device = device_id(principal)
    now, root = context.clock(), context.config.platform_root_key

    def attempt() -> Outcome:
        with context.pool.connection() as connection, connection.transaction():
            return register(
                connection, device, body, now=now, root_key=root, subagent_limits=context.config.subagent_limits
            )

    def run() -> Outcome:
        try:
            return attempt()
        except psycopg.errors.DeadlockDetected:
            return attempt()

    outcome = await asyncio.to_thread(run)
    await asyncio.to_thread(outcome.effects.flush)
    return outcome.body


def _task_wg(connection: Any, user: UUID, query: str, key: UUID) -> None:
    """`task:read` on the workgroup of the task/operation found by `query` (`404 not_found` otherwise)."""
    found = connection.execute(query, (key,)).fetchone()
    if found is None:
        raise HubError("not_found")
    rights.require(connection, user, rights.TASK_READ, wg_id=found[0])


def _list(context: HubContext, user: UUID, task: UUID, before: str | None, limit: int) -> dict[str, Any]:
    with context.pool.connection() as connection:
        _task_wg(connection, user, "SELECT wg_id FROM hub.task WHERE id = %s", task)
        return read.list_operations(connection, task, before=before, limit=limit)


@router.get("/v1/tasks/{task}/operations", auth="browser", right="task:read", read_resource="task.operations")
async def list_operations(
    context: Context, principal: CurrentPrincipal, task: str, before: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    count = parse_count(limit, read.LIST_DEFAULT, 1, read.LIST_MAX)
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_list, context, user, path_id(task), before, count)


def _get(context: HubContext, user: UUID, operation: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        _task_wg(connection, user, "SELECT wg_id FROM hub.operation WHERE id = %s", operation)
        return read.get_operation(connection, operation)


@router.get("/v1/operations/{id}", auth="browser", right="task:read", read_resource="operation")
async def get_operation(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_get, context, user, path_id(id))
