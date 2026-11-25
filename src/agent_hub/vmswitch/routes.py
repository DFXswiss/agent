"""Routes of the switch area (03-api.md section 4.5, ADR-093 section 5): `POST /v1/workgroups/{wg}/vms/{vm}/switch`
(`wg:read`, and `org:admin` of the workgroup's organization checked by `service.request_switch`)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from agent_hub.auth.fields import Empty
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.sessions.effects import Effects
from agent_hub.tasks.routes import VM_ID
from agent_hub.vmswitch import service
from agent_hub.workgroups.routes import path_id

router = HubRouter()


def _switch(context: HubContext, user: UUID, wg: UUID, vm: str) -> tuple[dict[str, Any], Effects]:
    effects = Effects()
    with transaction(context.pool) as connection:
        answer, device = service.request_switch(connection, user, wg, vm, context.clock())
        if device is not None:
            effects.hint(device, "reconcile", {})
    return answer, effects


@router.post("/v1/workgroups/{wg}/vms/{vm}/switch", auth="browser", right="wg:read")
async def post_switch(context: Context, principal: CurrentPrincipal, wg: str, vm: str, body: Empty) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    if VM_ID.fullmatch(vm) is None:
        raise HubError("not_found")
    answer, effects = await asyncio.to_thread(_switch, context, user, path_id(wg), vm)
    effects.flush()
    return answer
