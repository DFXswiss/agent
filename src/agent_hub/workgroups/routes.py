"""Routes of the workgroups area (03-api.md section 4.5): `GET /v1/workgroups/{wg}` (`wg:read`),
`GET /v1/workgroups/{wg}/device-candidates` (`wg:write`) and `GET /v1/orgs/{org}/devices` (`org:member`).

Reads never reveal foreign objects: an unknown, malformed or forbidden ID is `404 not_found` (03-api.md section 1).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from agent_hub.errors import HubError
from agent_hub.events.routes import parse_uuid
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.workgroups import read

router = HubRouter()


def path_id(text: str) -> UUID:
    """A path ID in canonical UUID form (`404 not_found` otherwise)."""
    try:
        return cast(UUID, parse_uuid(text))
    except ValueError:
        raise HubError("not_found") from None


def _workgroup(context: HubContext, user: UUID, wg: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        return read.workgroup(connection, wg, context.clock(), context.config.device_online_seconds)


def _candidates(context: HubContext, user: UUID, wg: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg)
        found = read.candidates(connection, wg, context.clock(), context.config.device_online_seconds)
    return {"candidates": found}


def _org_devices(context: HubContext, user: UUID, org: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        found = read.org_devices(connection, org, context.clock(), context.config.device_online_seconds)
    return {"devices": found}


@router.get("/v1/workgroups/{wg}", auth="browser", right="wg:read", read_resource="workgroup")
async def get_workgroup(context: Context, principal: CurrentPrincipal, wg: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_workgroup, context, user, path_id(wg))


@router.get(
    "/v1/workgroups/{wg}/device-candidates",
    auth="browser",
    right="wg:write",
    read_resource="workgroup.device_candidates",
)
async def get_device_candidates(context: Context, principal: CurrentPrincipal, wg: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_candidates, context, user, path_id(wg))


@router.get("/v1/orgs/{org}/devices", auth="browser", right="org:member", read_resource="org.devices")
async def get_org_devices(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_org_devices, context, user, path_id(org))
