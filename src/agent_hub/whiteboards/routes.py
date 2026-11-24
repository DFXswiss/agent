"""Routes of the whiteboards area (03-api.md section 4.9; writes go through `POST /v1/events`, HUB-06).

| Route | Right |
|---|---|
| `GET /v1/whiteboards?org&wg&task&level&before&limit` | `org:member` (org level) / `wg:read` (wg and task level) |
| `GET /v1/whiteboards/{entry}/history?before&limit` | the same right on the entry's level |

The list shows only entries the caller may read (`agent_hub.whiteboards.read`); a named `org` needs `org:member`, a
named `wg` `wg:read` and a named `task` `task:read` on its workgroup - `404 not_found` otherwise, like an unknown ID.
The history of an unknown entry or of one the caller may not read is `404 not_found` as well. Both are read-logged by
the pipeline (`read.whiteboards`, `read.whiteboard.history`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from agent_hub.errors import HubError
from agent_hub.events.routes import MAX_SEQ, parse_count, parse_uuid
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.whiteboards import read
from agent_hub.whiteboards.projection import ORG, TASK, WORKGROUP
from agent_hub.workgroups.routes import path_id

router = HubRouter()
LEVELS = frozenset({ORG, WORKGROUP, TASK})


def parse_filters(org: str | None, wg: str | None, task: str | None, level: str | None) -> read.Filters:
    """The list filters (`400 request_invalid` for a malformed ID or an unknown level)."""
    if level is not None and level not in LEVELS:
        raise HubError("request_invalid")
    try:
        return read.Filters(org=parse_uuid(org), wg=parse_uuid(wg), task=parse_uuid(task), level=level)
    except ValueError:
        raise HubError("request_invalid") from None


def check_filters(connection: Any, user: UUID, filters: read.Filters) -> None:
    """The rights on the named org, workgroup and task (`404 not_found`)."""
    if filters.org is not None:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=filters.org)
    if filters.wg is not None:
        rights.require(connection, user, rights.WG_READ, wg_id=filters.wg)
    if filters.task is not None:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (filters.task,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])


def _list(context: HubContext, user: UUID, filters: read.Filters, before: str | None, limit: int) -> dict[str, Any]:
    with context.pool.connection() as connection:
        check_filters(connection, user, filters)
        return read.list_entries(connection, user, filters, before=before, limit=limit)


@router.get("/v1/whiteboards", auth="browser", right=("org:member", "wg:read"), read_resource="whiteboards")
async def get_whiteboards(
    context: Context,
    principal: CurrentPrincipal,
    *,
    org: str | None = None,
    wg: str | None = None,
    task: str | None = None,
    level: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    filters = parse_filters(org, wg, task, level)
    count = parse_count(limit, read.LIST_DEFAULT, 1, read.LIST_MAX)
    return await asyncio.to_thread(_list, context, cast(UUID, principal.principal_id), filters, before, count)


def _history(context: HubContext, user: UUID, entry_id: UUID, before: int | None, limit: int | None) -> dict[str, Any]:
    with context.pool.connection() as connection:
        item = read.entry(connection, entry_id)
        if item is None:
            raise HubError("not_found")
        if read.is_org_level(item):
            rights.require(connection, user, rights.ORG_MEMBER, org_id=item.org_id)
        else:
            rights.require(connection, user, rights.WG_READ, wg_id=item.wg_id)
        return read.history(connection, item, before=before, limit=limit)


@router.get(
    "/v1/whiteboards/{entry}/history",
    auth="browser",
    right=("org:member", "wg:read"),
    read_resource="whiteboard.history",
)
async def get_history(
    context: Context, principal: CurrentPrincipal, entry: str, *, before: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    entry_id = path_id(entry)
    cursor = None if before is None else parse_count(before, 0, 0, MAX_SEQ)
    count = None if limit is None else parse_count(limit, read.LIST_DEFAULT, 1, read.LIST_MAX)
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_history, context, user, entry_id, cursor, count)
