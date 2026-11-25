"""`GET /v1/config` (anon): relying party, origin, platform root key, hub key, version, limits and the model allow-lists
of `AGENT_HUB_MODEL_CATALOG` (03-api.md sections 2, 4.1 and 12)."""

from __future__ import annotations

from typing import Any

from agent_core.signing import b64url_encode
from agent_hub import __version__
from agent_hub.config import ACCESS_KINDS, LIMITS
from agent_hub.http.context import Context, HubContext
from agent_hub.http.routing import HubRouter

router = HubRouter()


def client_config(context: HubContext) -> dict[str, Any]:
    """The body of `/v1/config`."""
    config = context.config
    catalog = config.model_catalog
    return {
        "rp_id": config.public.rp_id,
        "origin": config.public.origin,
        "platform_root_key": b64url_encode(config.platform_root_key),
        "hub_key": {"id": context.signer.key_id, "public_key": context.signer.public_key_text},
        "version": __version__,
        "limits": {**LIMITS, **config.subagent_limits.published()},
        "models": {kind: list(catalog.allowed[kind]) for kind in ACCESS_KINDS},
        "defaults": {"platform": catalog.platform_default},
    }


@router.get("/v1/config", auth="anon", right="anon")
async def get_config(context: Context) -> dict[str, Any]:
    return client_config(context)
