"""Routes of the main agents area (03-api.md section 4.6, ADR-095 section 5): `GET /v1/orgs/{org}/people/agents` and
`GET /v1/orgs/{org}/main-agents`.

Both need `org:member` (`404 not_found` for an unknown org, a malformed ID and a non-member alike, as the situation
view) and answer only tasks of the workgroups the caller reads (`agent_hub.mainagents.read`). Query values outside
their form are `400 request_invalid` with the pointer of the parameter; `owner` of the main agents is required. The
read log (`read.org.people.agents`, `read.org.main_agents`) is written by the pipeline.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, cast
from uuid import UUID

from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count, parse_uuid
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.mainagents import read
from agent_hub.orgs import rights
from agent_hub.workgroups.routes import path_id

router = HubRouter()


def _invalid(name: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [f"/{name}"]})


def _limit(text: str | None) -> int:
    try:
        return parse_count(text, read.LIST_DEFAULT, 1, read.LIST_MAX)
    except HubError:
        raise _invalid("limit") from None


def _id(text: str | None, name: str) -> UUID | None:
    try:
        return parse_uuid(text)
    except ValueError:
        raise _invalid(name) from None


def _people(context: HubContext, user: UUID, org: UUID, *, after: UUID | None, limit: int) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        return read.people(connection, org, user, after=after, limit=limit)


@router.get("/v1/orgs/{org}/people/agents", auth="browser", right="org:member", read_resource="org.people.agents")
async def people_agents(
    context: Context,
    principal: CurrentPrincipal,
    org: str,
    *,
    after: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    start, count = _id(after, "after"), _limit(limit)
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(functools.partial(_people, context, user, path_id(org), after=start, limit=count))


def _main_agents(
    context: HubContext, user: UUID, org: UUID, *, owner: UUID, before: str | None, limit: int
) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        return read.main_agents(connection, org, user, owner=owner, before=before, limit=limit)


@router.get("/v1/orgs/{org}/main-agents", auth="browser", right="org:member", read_resource="org.main_agents")
async def main_agents(
    context: Context,
    principal: CurrentPrincipal,
    org: str,
    *,
    owner: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    person = _id(owner, "owner")
    if person is None:
        raise _invalid("owner")
    count = _limit(limit)
    user = cast(UUID, principal.principal_id)
    call = functools.partial(_main_agents, context, user, path_id(org), owner=person, before=before, limit=count)
    return await asyncio.to_thread(call)
