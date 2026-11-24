"""Routes of the org invitations (03-api.md section 4.2): create, list and revoke (`agent_hub.invites.orgs`).

The request bodies are closed (unknown fields, wrong types: `400 request_invalid` with pointers); the entry itself is
checked by the log rules. After a committed create or revoke the route publishes the live frames of the append and
emits `log.appended` (`agent_hub.orgs.routes.publish_append`); a replay publishes nothing. The lookup of an
invitation is `POST /v1/invites/lookup` (`agent_hub.invites.lookup`, declared by the platform area).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from pydantic import StringConstraints

from agent_hub.auth.fields import Strict, decode
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.events import paging
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.invites import orgs
from agent_hub.orgs.routes import parse_id, publish_append, user_id

router = HubRouter()

_B64U = r"^(?:[A-Za-z0-9_-]{4})*(?:[A-Za-z0-9_-][AQgw]|[A-Za-z0-9_-]{2}[AEIMQUYcgkosw048])?$"
PackageCt = Annotated[str, StringConstraints(pattern=_B64U, min_length=38, max_length=87382)]


class CreateInvite(Strict):
    """`{entry, package_ct}`; the entry is checked by the log rules."""

    entry: dict[str, Any]
    package_ct: PackageCt


class RevokeInvite(Strict):
    """`{entry}` (`invite_revoke`)."""

    entry: dict[str, Any]


def _org(text: str) -> UUID:
    found = parse_id(text)
    if found is None:
        raise HubError("not_found")
    return found


async def _in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    def run() -> T:
        with transaction(context.pool) as connection:
            return work(connection)

    return await asyncio.to_thread(run)


def _answer(stored: orgs.Stored) -> None:
    if stored.result is not None:
        publish_append(stored.result)


@router.post("/v1/orgs/{org}/invites", auth="browser", right="org:admin")
async def create_invite(org: str, body: CreateInvite, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    org_id = _org(org)
    package = decode(body.package_ct)
    now = context.clock()
    root = context.config.platform_root_key
    stored = await _in_transaction(
        context,
        lambda c: orgs.create(
            c, org_id=org_id, user=user_id(principal), entry=body.entry, package=package, now=now, root_key=root
        ),
    )
    _answer(stored)
    return {"invite_id": stored.invite_id, "seq": stored.seq}


@router.get("/v1/orgs/{org}/invites", auth="browser", right="org:admin", read_resource="org.invites")
async def list_invites(
    org: str, context: Context, principal: CurrentPrincipal, before: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    org_id = _org(org)
    window = paging.window(before, limit, orgs.MAX_LISTED)
    now = context.clock()
    user = user_id(principal)
    return await _in_transaction(context, lambda c: orgs.listing(c, org_id=org_id, user=user, now=now, window=window))


@router.post("/v1/orgs/{org}/invites/{id}/revoke", auth="browser", right="org:admin")
async def revoke_invite(
    org: str, id: str, body: RevokeInvite, context: Context, principal: CurrentPrincipal
) -> dict[str, Any]:
    org_id = _org(org)
    now = context.clock()
    root = context.config.platform_root_key
    stored = await _in_transaction(
        context,
        lambda c: orgs.revoke(
            c, org_id=org_id, user=user_id(principal), invite_id=id, entry=body.entry, now=now, root_key=root
        ),
    )
    _answer(stored)
    return {"seq": stored.seq}
