"""The state of one run iteration that the control routes and the final checks see."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestrator.config import RunConfig
from orchestrator.node import Node
from orchestrator.services import Cluster, Double, Hub, Runner


@dataclass
class Context:
    config: RunConfig
    hub_node: Node
    runner_node: Node
    agent_python: str
    agent_src: str = ""
    core_on_runner: str = ""
    runner_run_dir: str = ""
    scratch: str = ""
    runner_scratch: str = ""
    bind: str = "127.0.0.1"
    peer: str = ""
    hub: Hub | None = None
    cluster: Cluster | None = None
    doubles: dict[str, Double] = field(default_factory=dict)
    runners: dict[str, Runner] = field(default_factory=dict)
    links: dict[str, Node] = field(default_factory=dict)
    control_url: str = ""
    notes: list[str] = field(default_factory=list)
    workvm_image: str = ""
    """The shared work image cache of the run (`orchestrator.workvm_image`), when a spec needs work VMs."""

    def state(self) -> dict[str, Any]:
        return {
            "topology": self.config.topology,
            "hub": {"origin": self.config.public_origin, "browser_relay": self.browser_address},
            "control": self.control_url,
            "doubles": {name: {"url": double.url} for name, double in self.doubles.items()},
            "runners": {name: runner.state() for name, runner in self.runners.items()},
            "links": sorted(self.links),
        }

    @property
    def browser_address(self) -> str:
        return f"127.0.0.1:{self.config.port('browser')}"
