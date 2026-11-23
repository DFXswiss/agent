"""routes/billing.py: the operator's billing command on the hub device and the start credit of the hub (KOR-31)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.routes import billing
from orchestrator.services import Hub
from support import CORE
from test_routes_model import FakeNode, call, ok


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def ctx(tmp_path: Path, node: FakeNode) -> Context:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    return Context(config, node, node, "/agent/python", scratch=str(tmp_path))


def test_post_billing_runs_the_command_with_the_hub_environment(ctx: Context, node: FakeNode, tmp_path: Path) -> None:
    assert call(ctx, "POST", "/hub/billing", {"args": ["show"]})[0] == 409
    ctx.hub = Hub(node, "/hub/python", {"A": "1"}, "127.0.0.1", 21006, sleep=lambda _s: None)
    node.respond = lambda _argv: {"rc": 0, "stdout": '{"org_id":"o"}\nnot json\n', "stderr": ""}
    assert call(ctx, "POST", "/hub/billing", {"args": ["show", "--org", "o"]}) == (
        200,
        {"rc": 0, "lines": [{"org_id": "o"}], "stderr": ""},
    )
    assert node.runs[-1] == (["/hub/python", "-m", "agent_hub", "billing", "show", "--org", "o"], {"A": "1"})
    # a price list is written to the scratch directory and stands for @price_list
    node.respond = lambda _argv: ok()
    status, _body = call(
        ctx, "POST", "/hub/billing", {"args": ["price-list", "add", "--file", "@price_list"], "price_list": {"a": 1}}
    )
    assert status == 200
    written = node.runs[-1][0][-1]
    assert written == str(tmp_path / "price-list-0.json")
    assert json.loads(Path(written).read_text(encoding="utf-8")) == {"a": 1}
    call(ctx, "POST", "/hub/billing", {"args": ["price-list", "add", "--file", "@price_list"], "price_list": {"b": 2}})
    assert node.runs[-1][0][-1] == str(tmp_path / "price-list-1.json")
    bad: object
    for bad in (None, {}, {"args": []}, {"args": ["a;b"]}, {"args": [1]}, {"args": ["x"], "price_list": []}):
        assert call(ctx, "POST", "/hub/billing", bad)[0] == 400


def test_post_start_credit_restarts_the_hub(ctx: Context, node: FakeNode) -> None:
    assert call(ctx, "POST", "/hub/start-credit", {"usd": "5"})[0] == 409
    ctx.hub = Hub(node, "/hub/python", {}, "127.0.0.1", 21006, sleep=lambda _s: None)
    assert call(ctx, "POST", "/hub/start-credit", {"usd": "5"}) == (200, {"usd": "5"})
    assert ctx.hub.env[billing.START_CREDIT] == "5"
    assert node.stopped == ["hub"]
    for bad in (None, {"usd": 5}, {"usd": "-1"}, {}):
        assert call(ctx, "POST", "/hub/start-credit", bad)[0] == 400
