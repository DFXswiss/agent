"""Routes of the sessions area (03-api.md section 4.6): `GET /v1/sessions/{session}` (`task:read`) and
`POST /v1/sessions/{session}/control` (`task:write`, `{action: "interrupt" | "stop"}`; the hint `session.control`
reaches the device, the runner reports the resulting transitions)."""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.tasks import read, service
from agent_hub.tasks.routes import _run
from agent_hub.workgroups.routes import path_id

router = HubRouter()


class ControlRequest(BaseModel):
    """Body of `POST /v1/sessions/{session}/control`."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["interrupt", "stop"]


def _session(context: HubContext, user: UUID, session: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.session WHERE id = %s", (session,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return read.get_session(connection, session)


@router.get("/v1/sessions/{session}", auth="browser", right="task:read", read_resource="session")
async def get_session(context: Context, principal: CurrentPrincipal, session: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_session, context, user, path_id(session))


@router.post("/v1/sessions/{session}/control", auth="browser", right="task:write")
async def control_session(
    context: Context, principal: CurrentPrincipal, session: str, body: ControlRequest
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_run, context, service.control, user, path_id(session), body.action)
