"""Routes of the monitoring area (03-api.md section 4.11): `GET /v1/monitoring/overview?org=` (`org:member`) and
`GET /v1/monitoring/series` (the rights of the overview for the entity). Semantics in `agent_hub.monitoring.read`.

Reads never reveal foreign objects: an unknown, malformed or invisible org or entity is `404 not_found`; a malformed
query parameter of the series is `400 request_invalid` with its pointer. The read log (`read.monitoring.overview`,
`read.monitoring.series`) is written by the pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from fastapi import Query

from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.monitoring import read
from agent_hub.workgroups.routes import path_id

router = HubRouter()


def _overview(context: HubContext, user: UUID, org: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        return read.overview(connection, org, user, context.clock(), context.config.device_online_seconds)


def _series(context: HubContext, user: UUID, params: dict[str, str | None]) -> dict[str, Any]:
    now = context.clock()
    query = read.parse_series(params, now)
    with context.pool.connection() as connection:
        if not read.entity_visible(connection, user, query.entity_kind, query.entity_id):
            raise HubError("not_found")
        return read.series(connection, query, now)


@router.get("/v1/monitoring/overview", auth="browser", right="org:member", read_resource="monitoring.overview")
async def get_overview(context: Context, principal: CurrentPrincipal, org: str | None = None) -> dict[str, Any]:
    if org is None:
        raise HubError("request_invalid", detail={"pointers": ["/org"]})
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_overview, context, user, path_id(org))


@router.get(
    "/v1/monitoring/series",
    auth="browser",
    right=("org:member", "wg:read"),
    read_resource="monitoring.series",
)
async def get_series(
    context: Context,
    principal: CurrentPrincipal,
    *,
    entity_kind: str | None = None,
    entity_id: str | None = None,
    metric: str | None = None,
    start: str | None = Query(default=None, alias="from"),
    end: str | None = Query(default=None, alias="to"),
    step: str | None = None,
) -> dict[str, Any]:
    params = {
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "metric": metric,
        "from": start,
        "to": end,
        "step": step,
    }
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_series, context, user, params)
