"""Route of the device outbox: `POST /v1/device/outbox` (03-api.md section 6.1, right `device` plus the item rights).

The device must be `active` (`agent_hub.device.keys.ActiveDevice`, decision HUB-08 2). The body is at most 4 MiB (the
route's body limit, `413 size_exceeded`) with 1 to 500 items (`400 request_invalid`); the pushes of a device are paced
by a token bucket of one push per 100 ms with a reserve of ten (`agent_hub.outbox.pacing`, `429 rate_limited`); the
runner starts a push at most every 100 ms and sends a late push once more at most every 2 s (ADR-050), so the copy
may arrive while the first request is processed or after later pushes (the device's stream row serialises them). The
push runs in one transaction in a worker thread (`agent_hub.outbox.process`); after the commit its frames, hints and
signals go out through `agent_hub.stream.publish` and `agent_hub.signals`, also in a worker thread (subscribers may do
I/O). A push that PostgreSQL aborts as a deadlock victim (the global audit lock meets row locks of other writers,
decision HUB-10 25) is run once more.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg

from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.outbox.bodies import OutboxRequest
from agent_hub.outbox.chains import chains
from agent_hub.outbox.pacing import pacer_of
from agent_hub.outbox.process import Outcome, process

router = HubRouter()
chains()  # a broken handler registration stops the start of the hub, never a push


@router.post("/v1/device/outbox", auth="device", right="device", max_body=EVENT_BATCH_BYTES)
async def post_outbox(context: Context, principal: ActiveDevice, body: OutboxRequest) -> dict[str, Any]:
    device = device_id(principal)
    retry_after = pacer_of(context.limiter, context.clock).admit(device)
    if retry_after is not None:
        raise HubError("rate_limited", headers={"Retry-After": str(retry_after)})
    now, root = context.clock(), context.config.platform_root_key

    def attempt() -> Outcome:
        with context.pool.connection() as connection, connection.transaction():
            return process(connection, device, body.items, now=now, root_key=root)

    def run() -> Outcome:
        try:
            return attempt()
        except psycopg.errors.DeadlockDetected:
            return attempt()

    outcome = await asyncio.to_thread(run)
    await asyncio.to_thread(outcome.flush)
    return outcome.answer()
