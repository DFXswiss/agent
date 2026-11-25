"""`POST /hub/subagent-limits`: restart the hub with other subagent limits (ADR-088 section 2; KOR-45, E2E-24, E2E-67).

Body `{"depth"?, "running"?, "total"?, "runtime_seconds"?}`: each a whole number 0-1000000 (0 = no limit) sets
`AGENT_HUB_SUBAGENT_MAX_<NAME>` for the restarted hub; a name left out goes back to the hub's default, so `{}` restores
every default. The database, the relays and the runners stay (they reconnect and read the new values with the device
state). Answers the limits the hub now publishes in `GET /v1/config`.
"""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError

VARIABLES = {
    "depth": "AGENT_HUB_SUBAGENT_MAX_DEPTH",
    "running": "AGENT_HUB_SUBAGENT_MAX_RUNNING",
    "total": "AGENT_HUB_SUBAGENT_MAX_TOTAL",
    "runtime_seconds": "AGENT_HUB_SUBAGENT_MAX_RUNTIME_SECONDS",
}
SETTING_MAX = 1_000_000


def _checked(body: Any) -> dict[str, int]:
    if not isinstance(body, dict) or not set(body) <= set(VARIABLES):
        raise RouteError(400, "invalid", f"body must be an object with some of {sorted(VARIABLES)}")
    for name, value in body.items():
        if type(value) is not int or not 0 <= value <= SETTING_MAX:
            raise RouteError(400, "invalid", f"{name} must be a whole number from 0 to {SETTING_MAX}")
    return body


def post_subagent_limits(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    wanted = _checked(body)
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    for name, variable in VARIABLES.items():
        if name in wanted:
            hub.env[variable] = str(wanted[name])
        else:
            hub.env.pop(variable, None)
    hub.node.stop("hub")
    hub.start()
    body = hub.node.http("GET", f"http://{hub.bind}:{hub.port}/v1/config", timeout=5)["body"]
    limits = body.get("limits") if isinstance(body, dict) else None
    if not isinstance(limits, dict):
        raise RouteError(502, "invalid", "the restarted hub answered no limits")
    return 200, {key: value for key, value in limits.items() if key.startswith("subagent_")}


ROUTES = [Route("POST", "/hub/subagent-limits", post_subagent_limits)]
