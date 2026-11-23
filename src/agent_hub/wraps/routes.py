"""Routes of the wraps area (03-api.md section 4.3): `GET/POST /v1/wraps` and `GET /v1/orgs/{org}/catch-up`.

`GET /v1/wraps` lists the caller's wraps newest first (`created_at`, then the slot); `POST /v1/wraps` stores wraps under
W3 (`agent_hub.wraps.rules`) and publishes the `wraps` frame/hint `{}` to every recipient of a new wrap;
`GET /v1/orgs/{org}/catch-up` lists the slots the calling member may fill (`agent_hub.wraps.catchup`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.log.wraps import CRED
from agent_hub.orgs import logs, rights
from agent_hub.orgs.routes import parse_id, user_id
from agent_hub.stream.publish import Audience, AudienceKind, publish
from agent_hub.wraps.catchup import catch_up
from agent_hub.wraps.rules import Stored, store_wraps, wraps_for

router = HubRouter()
KEY_SCOPES = frozenset({"org", "wg", CRED})
MAX_EPOCH = 2**31 - 1


class WrapsRequest(BaseModel):
    """Body of `POST /v1/wraps`."""

    model_config = ConfigDict(extra="forbid")
    wraps: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


def _list(context: HubContext, user: UUID, scope: str | None, scope_id: str | None, epoch: int | None) -> Any:
    with context.pool.connection() as connection:
        found = wraps_for(connection, user, scope=scope, scope_id=scope_id, epoch=epoch)
    return sorted(found, key=lambda wrap: wrap["created_at"], reverse=True)


@router.get("/v1/wraps", auth="browser", right="user", read_resource="wraps")
async def get_wraps(
    context: Context,
    principal: CurrentPrincipal,
    scope: str | None = None,
    scope_id: str | None = None,
    epoch: str | None = None,
) -> dict[str, Any]:
    if scope is not None and scope not in KEY_SCOPES:
        raise HubError("request_invalid")
    if scope_id is not None and parse_id(scope_id) is None:
        raise HubError("request_invalid")
    number = None
    if epoch is not None:
        if not (epoch.isascii() and epoch.isdigit()) or epoch != str(int(epoch)) or not 1 <= int(epoch) <= MAX_EPOCH:
            raise HubError("request_invalid")
        number = int(epoch)
    found = await asyncio.to_thread(_list, context, user_id(principal), scope, scope_id, number)
    return {"wraps": found}


def _store(context: HubContext, user: UUID, wraps: list[dict[str, Any]]) -> tuple[Stored, dict[str, str]]:
    with context.pool.connection() as connection, connection.transaction():
        stored = store_wraps(
            connection, wraps, wrapper=str(user), now=context.clock(), root_key=context.config.platform_root_key
        )
        kinds = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT id, kind FROM hub.principal WHERE id = ANY(%s::uuid[])", (sorted(stored.recipients),)
            )
        }
    return stored, kinds


@router.post("/v1/wraps", auth="browser", right="user", max_body=EVENT_BATCH_BYTES)
async def post_wraps(context: Context, principal: CurrentPrincipal, body: WrapsRequest) -> dict[str, Any]:
    stored, kinds = await asyncio.to_thread(_store, context, user_id(principal), body.wraps)
    for recipient in sorted(stored.recipients):
        kind: AudienceKind = "device" if kinds.get(recipient) == "device" else "user"
        publish(Audience(kind, UUID(recipient)), "wraps", {})
    return {"stored": stored.stored, "ignored": stored.ignored}


def _catch_up(context: HubContext, org_id: UUID, user: UUID) -> list[dict[str, Any]]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org_id)
        org = cast(logs.OrgRow, logs.org_row(connection, org_id))  # an active member implies the org
        slots = catch_up(connection, org, str(user), context.config.platform_root_key)
    return [slot.to_json() for slot in slots]


@router.get("/v1/orgs/{org}/catch-up", auth="browser", right="org:member", read_resource="org.catch_up")
async def get_catch_up(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    org_id = parse_id(org)
    if org_id is None:
        raise HubError("not_found")
    return {"missing": await asyncio.to_thread(_catch_up, context, org_id, user_id(principal))}
