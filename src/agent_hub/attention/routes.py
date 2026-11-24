"""Routes of the attention area (03-api.md section 4.6, ADR-064): `GET /v1/orgs/{org}/attention` and
`GET /v1/orgs/{org}/attention/summary`.

Both need `org:member` (`404 not_found` for an unknown org, a malformed ID and a non-member alike) and answer only
records of the workgroups the caller reads (`agent_hub.attention.read`). Query values outside their form are
`400 request_invalid` with the pointer of the parameter.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from agent_hub.attention import read
from agent_hub.attention.derive import CATEGORIES
from agent_hub.crypto.encoding import parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.workgroups.routes import path_id

router = HubRouter()
MINE = {None: False, "0": False, "1": True}


def _invalid(name: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [f"/{name}"]})


def parse_since(text: str | None) -> datetime | None:
    """The `since` parameter (contract time, `request_invalid` otherwise)."""
    if text is None:
        return None
    try:
        return parse_time(text)
    except CryptoError:
        raise _invalid("since") from None


def _list(context: HubContext, user: UUID, org: UUID, **options: Any) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        return read.list_attention(connection, org, user, **options)


@router.get("/v1/orgs/{org}/attention", auth="browser", right="org:member", read_resource="org.attention")
async def list_attention(
    context: Context,
    principal: CurrentPrincipal,
    org: str,
    *,
    category: str | None = None,
    mine: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise _invalid("category")
    if mine not in MINE:
        raise _invalid("mine")
    try:
        count = parse_count(limit, read.LIST_DEFAULT, 1, read.LIST_MAX)
    except HubError:
        raise _invalid("limit") from None
    options = {"category": category, "mine": MINE[mine], "since": parse_since(since), "before": before, "limit": count}
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_list, context, user, path_id(org), **options)


def _summary(context: HubContext, user: UUID, org: UUID, since: datetime | None) -> dict[str, int]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        return read.summary(connection, org, user, since, context.clock())


@router.get(
    "/v1/orgs/{org}/attention/summary", auth="browser", right="org:member", read_resource="org.attention.summary"
)
async def attention_summary(
    context: Context, principal: CurrentPrincipal, org: str, *, since: str | None = None
) -> dict[str, int]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_summary, context, user, path_id(org), parse_since(since))
