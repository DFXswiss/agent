"""`POST /hub/subagent-limits` (KOR-45, ADR-088 section 2): the hub restarted with other subagent limits."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.node import Node
from orchestrator.routes import hub_settings
from orchestrator.services import Hub
from support import CORE


class HubNode(Node):
    """Stops and spawns the hub; `/v1/config` answers the limits of the environment the hub was spawned with."""

    def __init__(self) -> None:
        self.name = "hub-node"
        self.env: dict[str, str] = {}
        self.stopped: list[str] = []
        self.config: Any = None

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "stop":
            self.stopped.append(args["name"])
            return 0
        if op == "spawn":
            self.env = dict(args["env"])
            return 1
        if op == "alive":
            return True
        if op == "http":
            if args["url"].endswith("/v1/health"):
                return {"status": 200, "body": {}}
            if self.config is not None:
                return {"status": 200, "body": self.config}
            limits = {name: int(self.env.get(variable, "7")) for name, variable in hub_settings.VARIABLES.items()}
            published = {f"subagent_max_{name}": value for name, value in limits.items()}
            return {"status": 200, "body": {"limits": {"request_body_bytes": 1, **published}}}
        raise AssertionError(op)


@pytest.fixture
def node() -> HubNode:
    return HubNode()


@pytest.fixture
def ctx(tmp_path: Path, node: HubNode) -> Context:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    return Context(config, node, node, "/agent/python", scratch=str(tmp_path))


def call(ctx: Context, body: Any) -> tuple[int, Any]:
    return dispatch(discover(), ctx, "POST", "/hub/subagent-limits", body)


def test_restarts_the_hub_with_the_limits_and_answers_what_it_publishes(ctx: Context, node: HubNode) -> None:
    assert call(ctx, {})[0] == 409
    ctx.hub = Hub(node, "/hub/python", {"A": "1"}, "127.0.0.1", 21006, sleep=lambda _s: None)
    status, body = call(ctx, {"depth": 2, "running": 3})
    assert status == 200
    assert body == {
        "subagent_max_depth": 2,
        "subagent_max_running": 3,
        "subagent_max_total": 7,
        "subagent_max_runtime_seconds": 7,
    }
    assert node.stopped == ["hub"]
    assert node.env["AGENT_HUB_SUBAGENT_MAX_DEPTH"] == "2"
    assert node.env["AGENT_HUB_SUBAGENT_MAX_RUNNING"] == "3"
    assert call(ctx, {"total": 0})[1]["subagent_max_total"] == 0
    assert "AGENT_HUB_SUBAGENT_MAX_DEPTH" not in ctx.hub.env  # left out: back to the default
    call(ctx, {})
    assert not any(name.startswith("AGENT_HUB_SUBAGENT_") for name in ctx.hub.env)
    assert ctx.hub.env["A"] == "1"


@pytest.mark.parametrize("body", [None, [], {"other": 1}, {"depth": -1}, {"depth": True}, {"running": 1_000_001}])
def test_refuses_other_bodies(ctx: Context, node: HubNode, body: Any) -> None:
    ctx.hub = Hub(node, "/hub/python", {}, "127.0.0.1", 21006, sleep=lambda _s: None)
    assert call(ctx, body)[0] == 400
    assert node.stopped == []


@pytest.mark.parametrize("answer", ["text", {"limits": []}, {}])
def test_a_hub_without_limits_is_an_error(ctx: Context, node: HubNode, answer: Any) -> None:
    ctx.hub = Hub(node, "/hub/python", {}, "127.0.0.1", 21006, sleep=lambda _s: None)
    node.config = answer
    assert call(ctx, {"depth": 1})[0] == 502
