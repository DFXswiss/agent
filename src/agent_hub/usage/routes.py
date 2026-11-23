"""`GET /v1/me/quotas` (03-api.md section 4.1): the subscription quota windows of the signed-in user.

Only the caller's own `quota_snapshot` rows (`owner_id` = the user), newest fetch first (then by window name). The read
log row `read.me.quotas` is written by the pipeline (`read_resource`). The request also binds the hub configuration
for the budget hook and the `quota` handler (`agent_hub.usage.binding`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from agent_hub.crypto.encoding import format_time
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.usage import binding

router = HubRouter()


def my_quotas(context: HubContext, user_id: UUID) -> list[dict[str, Any]]:
    """The quota windows of the user (module docstring)."""
    with context.pool.connection() as connection:
        rows = connection.execute(
            'SELECT "window", used_percent, resets_at, severity, fetched_at, device_id FROM hub.quota_snapshot'
            ' WHERE owner_id = %s ORDER BY fetched_at DESC, "window"',
            (user_id,),
        ).fetchall()
    return [
        {
            "window": window,
            "used_percent": used,
            "resets_at": None if resets is None else format_time(resets),
            "severity": severity,
            "fetched_at": format_time(fetched),
            "device_id": str(device),
        }
        for window, used, resets, severity, fetched, device in rows
    ]


@router.get("/v1/me/quotas", auth="browser", right="user", read_resource="me.quotas")
async def get_my_quotas(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    binding.bind(context.config)
    user = cast(UUID, principal.principal_id)
    return {"quotas": await asyncio.to_thread(my_quotas, context, user)}
