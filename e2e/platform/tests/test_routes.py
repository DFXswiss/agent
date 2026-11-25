"""Control routes against a context with stub nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import RouteError, discover, dispatch
from orchestrator.node import Node
from orchestrator.services import Cluster, Double, Hub, Runner
from support import CORE


class StubNode(Node):
    """Answers the calls the routes make; `runs` scripts `run` results by first argument after the module."""

    def __init__(self) -> None:
        self.name = "stub"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.alive_names: set[str] = set()

    def call(self, op: str, /, **args: Any) -> Any:  # noqa: PLR0911 - one answer per device method
        self.calls.append((op, args))
        if op == "http":
            return {"status": 200, "body": {"method": args["method"], "url": args["url"], "body": args["body"]}}
        if op == "relays":
            return {"model": {"cut": False, "connections": 1}, "other": {"cut": False}}
        if op in ("relay_cut", "relay_restore"):
            return {"cut": op == "relay_cut"}
        if op == "run":
            key = " ".join(args["argv"][3:5]) if len(args["argv"]) > 3 else ""
            return self.runs.get(key, {"rc": 0, "stdout": "", "stderr": ""})
        if op == "alive":
            return args["name"] in self.alive_names
        if op == "log_tail":
            return "runner.run tasks=2 enrolled=no"
        if op == "stop":
            self.alive_names.discard(args["name"])
            return -15
        if op == "kill":
            self.alive_names.discard(args["name"])
            return -9
        if op == "spawn":
            self.alive_names.add(args["name"])
            return 1
        if op == "wait_line":
            return "runner.run enrolled=no"
        raise AssertionError(op)


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    node = StubNode()
    context = Context(config, node, node, "/agent/python", control_url="http://127.0.0.1:21004", scratch=str(tmp_path))
    context.doubles["model"] = Double(node, "model", 21009, 21010)
    context.runners["runner-1"] = Runner(node, "runner-1", "/agent/python", {"A": "1"})
    node.alive_names.add("runner-1")
    context.links = {"model": node, "runner-hub": node}
    return context


ROUTES = discover()


def call(ctx: Context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return dispatch(ROUTES, ctx, method, path, body)


def test_state_and_doubles(ctx: Context) -> None:
    status, state = call(ctx, "GET", "/state")
    assert status == 200
    assert state["runners"] == {"runner-1": "waiting_for_enrollment"}
    assert state["doubles"] == {"model": {"url": "http://127.0.0.1:21010"}}
    assert state["links"] == [
        "model",
        "runner-hub",
    ]
    assert call(ctx, "GET", "/doubles/model/state")[1]["url"] == "http://127.0.0.1:21009/_double/state"
    assert call(ctx, "POST", "/doubles/model/scenario", {"packaged": "hello"})[1]["body"] == {"packaged": "hello"}
    assert call(ctx, "POST", "/doubles/model/scenario", ["x"])[0] == 400
    assert call(ctx, "POST", "/doubles/model/reset")[1]["url"].endswith("/_double/reset")
    status, answer = call(ctx, "POST", "/doubles/model/request", {"method": "POST", "path": "/v1/messages", "body": {}})
    assert status == 200
    assert answer["status"] == 200
    assert answer["body"]["url"].endswith("/v1/messages")
    assert call(ctx, "POST", "/doubles/model/request", {"method": "TRACE", "path": "/"})[0] == 400
    assert call(ctx, "POST", "/doubles/model/request", {"method": "GET", "path": "x"})[0] == 400
    assert call(ctx, "GET", "/doubles/github/state") == (404, {"error": "not_found", "detail": "no double github"})


def test_links(ctx: Context) -> None:
    assert call(ctx, "GET", "/links")[1] == {"model": {"cut": False, "connections": 1}}
    assert call(ctx, "POST", "/links/model/cut") == (200, {"cut": True})
    assert call(ctx, "POST", "/links/runner-hub/restore") == (200, {"cut": False})
    assert call(ctx, "POST", "/links/x/cut")[1]["detail"] == "no link x (links: model, runner-hub)"


def stopped(ctx: Context) -> bool:
    return ctx.runners["runner-1"].stopped_on_request


def test_runners(ctx: Context) -> None:
    assert call(ctx, "GET", "/runners")[1] == {"runner-1": "waiting_for_enrollment"}
    assert call(ctx, "POST", "/runners/runner-1/start")[0] == 400
    assert call(ctx, "POST", "/runners/runner-1/stop")[1] == {"name": "runner-1", "exit": -15, "state": "stopped"}
    assert stopped(ctx)
    assert call(ctx, "POST", "/runners/runner-1/start")[1]["state"] == "waiting_for_enrollment"
    assert not stopped(ctx)
    assert call(ctx, "POST", "/runners/runner-9/stop")[0] == 404
    # a hard kill (KOR-58): SIGKILL, the run's final checks do not expect the runner running
    assert call(ctx, "POST", "/runners/runner-1/hard-kill")[1] == {"name": "runner-1", "exit": -9, "state": "stopped"}
    assert stopped(ctx)
    assert ("kill", {"name": "runner-1"}) in ctx.runner_node.calls  # type: ignore[attr-defined]
    assert call(ctx, "POST", "/runners/runner-1/start")[0] == 200
    assert call(ctx, "POST", "/runners/runner-9/hard-kill")[0] == 404


def test_a_runner_restarts_with_its_github_ways(ctx: Context) -> None:
    runner = ctx.runners["runner-1"]
    call(ctx, "POST", "/runners/runner-1/stop")
    bad: dict[str, Any]
    for bad in ({"github": []}, {"github": {"other": "x"}}, {"github": {"app_slug": ""}}, {"github": {"app_slug": 5}}):
        assert call(ctx, "POST", "/runners/runner-1/start", bad)[0] == 400
    ways = {"oauth_client_id": "Ov23double00000000", "app_slug": "agent-dfx-swiss"}
    assert call(ctx, "POST", "/runners/runner-1/start", {"github": ways})[0] == 200
    assert runner.env["AGENT_RUNNER_GITHUB_OAUTH_CLIENT_ID"] == "Ov23double00000000"
    assert runner.env["AGENT_RUNNER_GITHUB_APP_SLUG"] == "agent-dfx-swiss"
    call(ctx, "POST", "/runners/runner-1/stop")
    assert call(ctx, "POST", "/runners/runner-1/start", {"github": {}})[0] == 200
    assert "AGENT_RUNNER_GITHUB_OAUTH_CLIENT_ID" not in runner.env
    assert "AGENT_RUNNER_GITHUB_APP_SLUG" not in runner.env


def test_operator_hub_db_and_faults(ctx: Context) -> None:
    node = ctx.hub_node
    assert isinstance(node, StubNode)
    assert call(ctx, "POST", "/operator", {"args": []})[0] == 400
    assert call(ctx, "POST", "/operator", {"args": ["invite-user"]})[0] == 501
    ctx.hub = Hub(node, "/hub/python", {"H": "1"}, "127.0.0.1", 21006)
    node.runs["platform --help"] = {"rc": 2, "stdout": "", "stderr": ""}
    assert call(ctx, "POST", "/operator", {"args": ["invite-user"]})[1]["error"] == "not_available"
    node.runs["platform --help"] = {"rc": 0, "stdout": "", "stderr": ""}
    node.runs["platform invite-user"] = {"rc": 0, "stdout": "code\n", "stderr": ""}
    assert call(ctx, "POST", "/operator", {"args": ["invite-user"]}) == (
        200,
        {"rc": 0, "stdout": "code\n", "stderr": ""},
    )
    assert call(ctx, "POST", "/hub-db/query", {"sql": "SELECT 1; DROP TABLE x"})[0] == 400
    assert call(ctx, "POST", "/hub-db/query", {"sql": "SELECT 1"})[0] == 409
    ctx.cluster = Cluster(node, "/pg", "/data", 21005, {})
    node.runs["-A -t"] = {"rc": 0, "stdout": '[{"n": 1}]\n', "stderr": ""}
    assert call(ctx, "POST", "/hub-db/query", {"sql": "SELECT 1 AS n;"}) == (200, {"rows": [{"n": 1}]})
    psql = next(args for op, args in node.calls if op == "run" and args["argv"][0] == "/pg/psql")
    assert psql["env"]["PGOPTIONS"] == "-c default_transaction_read_only=on"
    assert call(ctx, "POST", "/clock/advance", {"seconds": True})[0] == 400
    assert call(ctx, "POST", "/clock/advance", {"seconds": 60})[0] == 501
    assert call(ctx, "POST", "/hooks/fault", {"status": 404})[0] == 400
    assert call(ctx, "POST", "/hooks/fault", {"status": None}) == (200, {"status": None, "vms": []})


def test_route_error_shape() -> None:
    error = RouteError(409, "invalid", "busy")
    assert (error.status, error.code, str(error)) == (409, "invalid", "busy")
