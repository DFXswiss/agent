"""The closing checks of every E2E spec (07-testing.md section 2.1) at the end of the spec itself, so a failure names
the spec that caused it (the run repeats them after the iteration):

- `GET /checks/egress`: the egress violations recorded so far on the run's devices (connections that left a device
  to anything but the doubles, the hub and the other device; sampler failures) → `{violations: [...], records: n}`.
- `POST /checks/audit`: `agent-hub audit verify` on the hub device → `{rc, stdout}` (exit 0: the chain verifies).
"""

from __future__ import annotations

from typing import Any

from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.egress import violations


def get_egress(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    records: list[dict[str, Any]] = []
    for node in dict.fromkeys((ctx.hub_node, ctx.runner_node)):
        records += [dict(record, device=node.name) for record in node.egress_records()]
    return 200, {"violations": violations(records), "records": len(records)}


def post_audit(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    hub = ctx.hub
    if hub is None:
        raise RouteError(409, "invalid", "the hub is not running")
    done = hub.node.run([hub.python, "-m", "agent_hub", "audit", "verify"], env=hub.env)
    return 200, {"rc": done["rc"], "stdout": done["stdout"].strip()[-2000:]}


ROUTES = [Route("GET", "/checks/egress", get_egress), Route("POST", "/checks/audit", post_audit)]
