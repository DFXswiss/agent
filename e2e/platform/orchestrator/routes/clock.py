"""`POST /clock/advance` (`{"seconds": n}`): advance the fake clocks of hub and runner (expiry tests).

Neither the hub nor the runner process has a fake-clock hook (`agent_hub.clock` is injected in unit tests only), so
the route answers `501 not_available` after checking the body. The expiry scenario E2E-03 runs on real time instead
(decisions/SCN-03.md item 1: short expiry, the hub's expiry job polled); a fake clock would be a product change."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError, not_available


def post_clock(_ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    seconds = body.get("seconds") if isinstance(body, dict) else None
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise RouteError(400, "invalid", 'body must be {"seconds": <positive integer>}')
    raise not_available("advancing the fake clocks of hub and runner", "a fake-clock hook in hub and runner")


ROUTES = [Route("POST", "/clock/advance", post_clock)]
