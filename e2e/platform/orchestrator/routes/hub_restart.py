"""`POST /hub/restart`: stop and start the hub process with its unchanged environment (KOR-36, E2E-39).

A release restarts the hub while a browser may be in the middle of a flow; the scenario checks that the flow can be
repeated afterwards in the same tab. Answers the hub's state after the start (the start waits until it is ready)."""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError


def post_restart(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    hub.node.stop("hub")
    hub.start()  # a hub that does not come up raises ServiceError: the control server answers 500
    return 200, {"restarted": True}


ROUTES = [Route("POST", "/hub/restart", post_restart)]
