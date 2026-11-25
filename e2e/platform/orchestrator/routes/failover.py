"""`POST /hub/failover` (`{"online_seconds": n | null, "failover_seconds": m | null}`): restarts the hub with
`AGENT_HUB_DEVICE_ONLINE_SECONDS` and `AGENT_HUB_DEVICE_FAILOVER_SECONDS` (KOR-58, ADR-087 section 1; E2E-67: a device
failure within a test instead of 5 minutes). `null` (or a missing member) removes the setting, so the hub's default
applies again (30 and 300 s). The values follow the hub's own rule - online 1..3600, failover 1..86400 and at least
twice the online window that applies - so the hub never refuses to start; the runners send a heartbeat every 15 s, so
an online window below that makes connected devices look offline (the spec's business). The database, the relays and
the runners stay (they reconnect). Answers both values as set."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

ONLINE = "AGENT_HUB_DEVICE_ONLINE_SECONDS"
FAILOVER = "AGENT_HUB_DEVICE_FAILOVER_SECONDS"
DEFAULT_ONLINE = 30
DEFAULT_FAILOVER = 300
LIMITS = {"online_seconds": 3600, "failover_seconds": 86400}
VARIABLES = {"online_seconds": ONLINE, "failover_seconds": FAILOVER}


def _seconds(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIMITS[key]:
        raise RouteError(400, "invalid", f"{key} must be an integer from 1 to {LIMITS[key]} or null")
    return value


def post_failover(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    if not isinstance(body, dict) or not set(body) <= set(VARIABLES):
        raise RouteError(400, "invalid", 'body must be {"online_seconds"?: n | null, "failover_seconds"?: n | null}')
    values = {key: _seconds(body, key) for key in VARIABLES}
    online = values["online_seconds"] or DEFAULT_ONLINE
    if (values["failover_seconds"] or DEFAULT_FAILOVER) < 2 * online:
        raise RouteError(400, "invalid", f"failover_seconds must be at least twice the online window ({2 * online})")
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    for key, variable in VARIABLES.items():
        if values[key] is None:
            hub.env.pop(variable, None)
        else:
            hub.env[variable] = str(values[key])
    hub.node.stop("hub")
    hub.start()
    return 200, values


ROUTES = [Route("POST", "/hub/failover", post_failover)]
