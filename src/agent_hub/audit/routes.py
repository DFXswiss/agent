"""Routes of the audit area (03-api.md section 4.11): `GET /v1/orgs/{org}/audit` (`org:admin` every row of the org,
`org:member` the rows of readable workgroups and the member's own actions) and `GET /v1/orgs/{org}/audit/verify`
(`org:admin`). Semantics in `agent_hub.audit.query`; unknown, malformed and forbidden orgs are `404 not_found`. The
read log (`read.org.audit`, `read.org.audit.verify`) is written by the pipeline, after the response - a verification
does not see its own read row. Verifications run one at a time per process (they recompute the whole chain).
"""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import Any, cast
from uuid import UUID

from agent_hub.audit import query
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.workgroups.routes import path_id

router = HubRouter()
# A verification recomputes the whole chain; one at a time per process bounds the load repeated requests can cause.
_VERIFYING = threading.Lock()


def _list(
    context: HubContext, user: UUID, org: UUID, *, filters: dict[str, Any], before: str | None, limit: int
) -> dict[str, Any]:
    with context.pool.connection() as connection:
        return query.list_audit(connection, org, user, filters=filters, before=before, limit=limit)


def _verify(context: HubContext, user: UUID, org: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_ADMIN, org_id=org)
    with _VERIFYING, context.pool.connection() as connection:
        return query.verify(connection, context.signer.key_id, context.signer.public_key)


@router.get("/v1/orgs/{org}/audit", auth="browser", right=("org:admin", "org:member"), read_resource="org.audit")
async def get_audit(
    context: Context,
    principal: CurrentPrincipal,
    org: str,
    *,
    before: str | None = None,
    limit: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    task: str | None = None,
    session: str | None = None,
    wg: str | None = None,
) -> dict[str, Any]:
    org_id = path_id(org)
    count = parse_count(limit, query.LIST_DEFAULT, 1, query.LIST_MAX)
    filters = query.parse_filters({"action": action, "actor": actor, "task": task, "session": session, "wg": wg})
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(
        functools.partial(_list, context, user, org_id, filters=filters, before=before, limit=count)
    )


@router.get("/v1/orgs/{org}/audit/verify", auth="browser", right="org:admin", read_resource="org.audit.verify")
async def get_audit_verify(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_verify, context, user, path_id(org))
