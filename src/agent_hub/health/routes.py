"""`GET /v1/health` (anon): `{"ok": true, "version": ...}` while the database answers, else `503 unavailable`."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_hub import __version__
from agent_hub.errors import HubError
from agent_hub.http.context import Context, HubContext
from agent_hub.http.routing import HubRouter

PROBE_TIMEOUT_SECONDS = 2.0

router = HubRouter()


def _probe(context: HubContext) -> None:
    with context.pool.connection(timeout=PROBE_TIMEOUT_SECONDS) as connection:
        connection.execute("SELECT 1")


@router.get("/v1/health", auth="anon", right="anon")
async def health(context: Context) -> dict[str, Any]:
    try:
        await asyncio.to_thread(_probe, context)
    except Exception:
        raise HubError("unavailable") from None
    return {"ok": True, "version": __version__}
