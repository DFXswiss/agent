"""Routes of the support area (03-api.md section 4.14, ADR-096).

- `GET /v1/support/releases` (browser, `user`): the active releases for an active support person - `{releases:
  [{org_id, wg_id, access, granted_seq, granted_at}], truncated}`, the newest `RELEASES_MAX` first (`truncated` when
  more exist, ADR-086 section 2); `404 not_found` for anybody else (a user learns nothing about releases it may not
  read);
- `GET /v1/orgs/{org}/support` (browser, `org:member`): the organization's active releases and the current support
  persons with their display names (the release dialog names them) - `{releases: [{wg_id, access, granted_seq,
  granted_at}], people: [{user_id, name}]}`;
- `PUT /v1/platform/support/{user}` (root): `{name}` sets the display name of a person of the roster (`404 not_found`
  for a user that never held the role); audited as `support.name` without the name.

Releasing and revoking go through the membership log (`POST /v1/orgs/{org}/log`, entries `support_grant` and
`support_revoke` with their wraps and the `support.info` event); a support person reads and writes through the
ordinary workgroup and task routes (`agent_hub.orgs.rights`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.crypto.encoding import format_time
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.support import store

router = HubRouter()
RELEASES_MAX = 1000


class NameRequest(BaseModel):
    """Body of `PUT /v1/platform/support/{user}`."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")


def _uuid(text: str) -> UUID:
    try:
        value = UUID(text)
    except ValueError:
        raise HubError("not_found") from None
    if str(value) != text:
        raise HubError("not_found")
    return value


def releases(connection: Any, user: UUID) -> dict[str, Any]:
    """The body of `GET /v1/support/releases`."""
    if not store.is_person(connection, user):
        raise HubError("not_found")
    rows = connection.execute(
        "SELECT org_id, wg_id, access, granted_seq, granted_at FROM hub.support_release"
        " ORDER BY granted_at DESC, wg_id LIMIT %s",
        (RELEASES_MAX + 1,),
    ).fetchall()
    return {
        "truncated": len(rows) > RELEASES_MAX,
        "releases": [
            {
                "org_id": str(org),
                "wg_id": str(wg),
                "access": access,
                "granted_seq": seq,
                "granted_at": format_time(at),
            }
            for org, wg, access, seq, at in rows[:RELEASES_MAX]
        ],
    }


def org_support(connection: Any, org: UUID, user: UUID) -> dict[str, Any]:
    """The body of `GET /v1/orgs/{org}/support`."""
    rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
    rows = connection.execute(
        "SELECT wg_id, access, granted_seq, granted_at FROM hub.support_release WHERE org_id = %s ORDER BY wg_id",
        (org,),
    ).fetchall()
    people = connection.execute("SELECT user_id, name FROM hub.support_person WHERE active ORDER BY user_id").fetchall()
    return {
        "releases": [
            {"wg_id": str(wg), "access": access, "granted_seq": seq, "granted_at": format_time(at)}
            for wg, access, seq, at in rows
        ],
        "people": [{"user_id": str(person), "name": name} for person, name in people],
    }


def set_name(connection: Any, user: UUID, name: str, at: Any) -> dict[str, Any]:
    """The body of `PUT /v1/platform/support/{user}`."""
    found = connection.execute(
        "UPDATE hub.support_person SET name = %s WHERE user_id = %s RETURNING active", (name, user)
    ).fetchone()
    if found is None:
        raise HubError("not_found")
    append_audit(
        connection,
        AuditRow(
            at=at,
            actor_kind="operator",
            action="support.name",
            result="ok",
            target_kind="user",
            target_id=str(user),
        ),
    )
    return {"user_id": str(user), "name": name, "active": bool(found[0])}


def _run(context: HubContext, work: Any) -> dict[str, Any]:
    with transaction(context.pool) as connection:
        return cast(dict[str, Any], work(connection))


@router.get("/v1/support/releases", auth="browser", right="user", read_resource="support.releases")
async def get_releases(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_run, context, lambda connection: releases(connection, user))


@router.get("/v1/orgs/{org}/support", auth="browser", right="org:member", read_resource="org.support")
async def get_org_support(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    org_id, user = _uuid(org), cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_run, context, lambda connection: org_support(connection, org_id, user))


@router.put("/v1/platform/support/{user}", auth="root", right="root")
async def put_name(context: Context, user: str, body: NameRequest) -> dict[str, Any]:
    person, now = _uuid(user), context.clock()
    return await asyncio.to_thread(_run, context, lambda connection: set_name(connection, person, body.name, now))
