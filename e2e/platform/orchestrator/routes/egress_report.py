"""`GET /egress/report`: the egress report of the role topology E2E-32 (`orchestrator/egress_report.py`) for the run so
far - both devices' egress records, the run's named ports and the command lines of every process the run started on
each device (for the tunnel check). Answers `{topology, hub, devices, problems}`; the spec stores it as an artefact and
asserts no problem."""

from __future__ import annotations

from typing import Any

from orchestrator.config import PORT_OFFSETS
from orchestrator.context import Context
from orchestrator.control import Route, RouteError
from orchestrator.egress import PS
from orchestrator.egress_report import Roles, build, commands
from orchestrator.node import Node


def _commands(node: Node) -> list[str]:
    roots = [int(info["pid"]) for info in node.processes().values() if info.get("alive")]
    done = node.run(PS, env={"PATH": "/usr/bin:/bin"})
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"ps on {node.name} exited with {done['rc']}")
    return commands(str(done["stdout"]), roots)


def get_report(ctx: Context, _params: dict[str, str], _body: Any) -> tuple[int, Any]:
    config = ctx.config
    ports = {config.port(name): name for name in PORT_OFFSETS}
    roles = Roles(config.topology, ctx.bind, config.port("hub"), config.public_origin, ctx.peer, ports)
    nodes = list(dict.fromkeys((ctx.hub_node, ctx.runner_node)))
    records = {node.name: node.egress_records() for node in nodes}
    return 200, build(roles, records, {node.name: _commands(node) for node in nodes})


ROUTES = [Route("GET", "/egress/report", get_report)]
