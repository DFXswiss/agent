"""Control routes of SCN-03: further runner instances (spawn, remove) and their read-only CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.control import discover, dispatch
from orchestrator.node import Node
from orchestrator.services import Cluster, Hub, Runner
from support import CORE

PREFIX = "wp-t-e2e-"


class InstanceNode(Node):
    """Processes by name; `run` records argv and answers `limactl list` from `vms` and the runner CLI from `cli`."""

    def __init__(self) -> None:
        self.name = "instances"
        self.runs: list[tuple[list[str], dict[str, str] | None]] = []
        self.alive_names: set[str] = set()
        self.spawned: dict[str, dict[str, str]] = {}
        self.vms: list[str] = []
        self.cli: dict[str, Any] = {"rc": 0, "stdout": "runner.doctor result=ok\n", "stderr": ""}

    def call(self, op: str, /, **args: Any) -> Any:  # noqa: PLR0911 - one answer per device method
        if op == "run":
            argv = args["argv"]
            self.runs.append((argv, args["env"]))
            if argv[:2] == ["limactl", "list"]:
                listing = "".join(json.dumps({"name": n, "status": "Running", "memory": 0}) + "\n" for n in self.vms)
                return {"rc": 0, "stdout": listing, "stderr": ""}
            if argv[:3] == ["limactl", "delete", "-f"]:
                self.vms.remove(argv[3])
            if argv[1:3] == ["-m", "agent_runner"] and argv[3] in ("doctor", "status"):
                return self.cli
            return {"rc": 0, "stdout": "", "stderr": ""}
        if op == "alive":
            return args["name"] in self.alive_names
        if op == "log_tail":
            return "runner.run tasks=2 enrolled=no"
        if op == "stop":
            self.alive_names.discard(args["name"])
            return -15
        if op == "spawn":
            self.alive_names.add(args["name"])
            self.spawned[args["name"]] = args["env"]
            return 1
        if op == "wait_line":
            return "runner.run enrolled=no"
        raise AssertionError(op)


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    config = RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    node = InstanceNode()
    context = Context(config, node, node, "/agent/python", scratch=str(tmp_path), runner_scratch="/scratch")
    env = {
        "AGENT_RUNNER_OPENROUTER_KEY_FILE": "/scratch/platform-openrouter.key",
        "AGENT_RUNNER_HOME": "/scratch/runner-1",
        "AGENT_RUNNER_VM_PREFIX": f"{PREFIX}r1-",
        "AGENT_RUNNER_POOL_SIZE": "2",
    }
    context.runners["runner-1"] = Runner(node, "runner-1", "/agent/python", env)
    node.alive_names.add("runner-1")
    return context


ROUTES = discover()


def call(ctx: Context, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    return dispatch(ROUTES, ctx, method, path, body)


def node_of(ctx: Context) -> InstanceNode:
    node = ctx.runner_node
    assert isinstance(node, InstanceNode)
    return node


def test_spawn_copies_runner_1_with_own_home_prefix_and_capacity(ctx: Context) -> None:
    ctx.notes += ["pool size 2", "golden image cache-golden-abc"]
    options = {
        "memory_reserve_mib": 0,
        "pool_size": 1,
        "session_idle_seconds": 60,
        "vm_idle_seconds": 90,
        "sessions_per_vm": 1,
        "max_vms": 2,
    }
    status, answer = call(ctx, "POST", "/runners/runner-2/spawn", options)
    assert status == 200
    assert answer == {
        "name": "runner-2",
        "state": "waiting_for_enrollment",
        "vm_prefix": f"{PREFIX}r2-",
        "home": "/scratch/runner-2",
    }
    node = node_of(ctx)
    env = node.spawned["runner-2"]
    assert env["AGENT_RUNNER_HOME"] == "/scratch/runner-2"
    assert env["AGENT_RUNNER_VM_PREFIX"] == f"{PREFIX}r2-"
    assert (env["AGENT_RUNNER_SESSIONS_PER_VM"], env["AGENT_RUNNER_MAX_VMS"]) == ("1", "2")
    assert env["AGENT_RUNNER_MEMORY_RESERVE_MIB"] == "0"
    assert env["AGENT_RUNNER_POOL_SIZE"] == "1"
    assert (env["AGENT_RUNNER_SESSION_IDLE_SECONDS"], env["AGENT_RUNNER_VM_IDLE_SECONDS"]) == ("60", "90")
    assert ctx.runners["runner-1"].env["AGENT_RUNNER_HOME"] == "/scratch/runner-1"
    adopt = [argv for argv, _ in node.runs if argv[3:5] == ["image", "adopt"]]
    assert adopt == [["/agent/python", "-m", "agent_runner", "image", "adopt", "cache-golden-abc"]]
    assert call(ctx, "GET", "/runners")[1] == {
        "runner-1": "waiting_for_enrollment",
        "runner-2": "waiting_for_enrollment",
    }
    assert call(ctx, "POST", "/runners/runner-2/spawn")[1]["detail"] == "runner runner-2 exists"


def test_spawn_defaults_without_golden(ctx: Context) -> None:
    assert call(ctx, "POST", "/runners/runner-3/spawn")[0] == 200
    node = node_of(ctx)
    env = node.spawned["runner-3"]
    assert env["AGENT_RUNNER_OPENROUTER_KEY_FILE"] == "/scratch/platform-openrouter.key"
    assert "AGENT_RUNNER_MEMORY_RESERVE_MIB" not in env
    assert "AGENT_RUNNER_VM_IDLE_SECONDS" not in env
    assert env["AGENT_RUNNER_POOL_SIZE"] == "2"
    assert node.runs == []
    assert call(ctx, "POST", "/runners/runner-4/spawn", {"device": "org"})[0] == 200
    assert "AGENT_RUNNER_OPENROUTER_KEY_FILE" not in node.spawned["runner-4"]


@pytest.mark.parametrize(
    ("name", "body", "detail"),
    [
        ("runner-1", None, "a further runner is named runner-<2..9>"),
        ("runner-x", None, "a further runner is named runner-<2..9>"),
        ("runner-2", ["x"], "body must be an object of spawn options"),
        ("runner-2", {"vm_idle_seconds": 59}, "vm_idle_seconds must be 60..604800 or null"),
        ("runner-2", {"session_idle_seconds": "60"}, "session_idle_seconds must be 60..604800 or null"),
        ("runner-2", {"device": "hub"}, "device must be platform or org"),
        ("runner-2", {"memory_reserve_mib": -1}, "memory_reserve_mib must be 0..4194304 or null"),
        ("runner-2", {"memory_reserve_mib": True}, "memory_reserve_mib must be 0..4194304 or null"),
        ("runner-2", {"pool_size": 9}, "pool_size must be 0..8 or null"),
        ("runner-2", {"pool_size": "1"}, "pool_size must be 0..8 or null"),
        ("runner-2", {"sessions_per_vm": 1001}, "sessions_per_vm must be 0..1000 or null"),
        ("runner-2", {"max_vms": -1}, "max_vms must be 0..1000 or null"),
    ],
)
def test_spawn_refusals(ctx: Context, name: str, body: Any, detail: str) -> None:
    assert call(ctx, "POST", f"/runners/{name}/spawn", body) == (400, {"error": "invalid", "detail": detail})


def test_spawn_needs_runner_1(ctx: Context) -> None:
    del ctx.runners["runner-1"]
    status, answer = call(ctx, "POST", "/runners/runner-2/spawn")
    assert (status, answer["detail"]) == (409, "no runner-1 to copy the environment from")


def test_remove_stops_deletes_its_vms_and_home(ctx: Context) -> None:
    node = node_of(ctx)
    assert call(ctx, "POST", "/runners/runner-2/remove")[0] == 404
    call(ctx, "POST", "/runners/runner-2/spawn")
    node.vms = [f"{PREFIX}r2-arp-1", f"{PREFIX}r1-arp-1", f"{PREFIX}r2-wg-2"]
    assert call(ctx, "POST", "/runners/runner-2/remove") == (
        200,
        {"name": "runner-2", "deleted": [f"{PREFIX}r2-arp-1", f"{PREFIX}r2-wg-2"]},
    )
    assert node.vms == [f"{PREFIX}r1-arp-1"]
    assert node.runs[-1][0] == ["rm", "-rf", "/scratch/runner-2"]
    assert "runner-2" not in ctx.runners
    assert "runner-2" not in node.alive_names
    assert call(ctx, "POST", "/runners/runner-1/remove")[0] == 400


def test_cli_runs_only_read_only_commands(ctx: Context) -> None:
    assert call(ctx, "POST", "/runners/runner-1/cli", {"args": ["doctor"]}) == (
        200,
        {"rc": 0, "stdout": "runner.doctor result=ok\n", "stderr": ""},
    )
    argv, env = node_of(ctx).runs[-1]
    assert argv == ["/agent/python", "-m", "agent_runner", "doctor"]
    assert env == ctx.runners["runner-1"].env
    assert call(ctx, "POST", "/runners/runner-1/cli", {"args": ["status", "--json"]})[0] == 200
    for body in ({"args": ["enroll"]}, {"args": "doctor"}, None):
        status, answer = call(ctx, "POST", "/runners/runner-1/cli", body)
        assert (status, answer["detail"]) == (400, "args must be one of: doctor, status, status --json")
    assert call(ctx, "POST", "/runners/runner-7/cli", {"args": ["doctor"]})[0] == 404


# -- spec checks ---------------------------------------------------------------------------------------------------


class EgressNode(InstanceNode):
    def __init__(self, name: str, records: list[dict[str, Any]]) -> None:
        super().__init__()
        self.name = name
        self.records = records

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "egress_records":
            return self.records
        if op == "run":
            self.runs.append((args["argv"], args["env"]))
            return {"rc": 1, "stdout": "  audit chain broken at seq 7\n", "stderr": ""}
        return super().call(op, **args)


def test_spec_checks_egress_and_audit(tmp_path: Path) -> None:
    config = RunConfig(
        topology="t2", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )
    bad = {"source": "sampler", "allowed": False, "remote": "203.0.113.9:443"}
    hub_node = EgressNode("hub-device", [{"source": "sampler", "allowed": True}, bad])
    runner_node = EgressNode("runner-device", [{"event": "open", "link": "model"}])
    ctx = Context(config, hub_node, runner_node, "/agent/python")
    assert call(ctx, "GET", "/checks/egress") == (
        200,
        {"violations": [dict(bad, device="hub-device")], "records": 3},
    )
    assert call(ctx, "POST", "/checks/audit")[0] == 409
    ctx.hub = Hub(hub_node, "/hub/python", {"H": "1"}, "127.0.0.1", 21006)
    assert call(ctx, "POST", "/checks/audit") == (200, {"rc": 1, "stdout": "audit chain broken at seq 7"})
    assert hub_node.runs[-1] == (["/hub/python", "-m", "agent_hub", "audit", "verify"], {"H": "1"})
    same = Context(config, hub_node, hub_node, "/agent/python")
    assert call(same, "GET", "/checks/egress")[1]["records"] == 2


# -- hub tamper ----------------------------------------------------------------------------------------------------

ORG_ID = "01a20172-b86a-789c-89ab-7aa8af7fa991"
WG_ID = "01a20172-b86a-789c-89ab-7aa8af7fa992"
AUTHOR = "01a20172-b86a-789c-89ab-7aa8af7fa993"
EVENT_ID = "01a20172-b86a-789c-89ab-7aa8af7fa994"


def forged(**header: Any) -> dict[str, Any]:
    base = {
        "id": EVENT_ID,
        "org": ORG_ID,
        "scope": "wg",
        "scope_id": WG_ID,
        "epoch": 1,
        "author": AUTHOR,
        "kind": "whiteboard.entry",
        "ts": "2026-11-22T10:00:00.123Z",
        "lh": 4,
        "wg": WG_ID,
        "task": "",
        "ref": "it's",
    }
    return {"header": {**base, **header}, "ct": "AAEC", "sig": "_-8"}


class PsqlNode(InstanceNode):
    def call(self, op: str, /, **args: Any) -> Any:
        if op == "run" and args["argv"][0] == "/pg/psql":
            self.runs.append((args["argv"], args["env"]))
            return {"rc": 0, "stdout": "7\n", "stderr": ""}
        return super().call(op, **args)


def test_forge_event_stores_a_refused_event_as_superuser(ctx: Context) -> None:
    node = PsqlNode()
    ctx.hub_node = ctx.runner_node = node
    body = {"event": forged(), "author_kind": "user"}
    assert call(ctx, "POST", "/hub-db/forge-event", body)[0] == 409
    ctx.cluster = Cluster(node, "/pg", "/data", 21005, {}, password="pw")
    assert call(ctx, "POST", "/hub-db/forge-event", body) == (200, {"scope_seq": 7})
    argv, env = node.runs[-1]
    assert argv[argv.index("-U") + 1] == "postgres"
    assert env is not None
    assert "PGOPTIONS" not in env
    sql = argv[-1]
    assert "INSERT INTO hub.scope_head" in sql
    assert "RETURNING scope_seq" in sql
    assert f"'{WG_ID}', h.seq, '{EVENT_ID}', 'wg', '{ORG_ID}', '{WG_ID}', NULL, NULL, '{AUTHOR}', 'user'" in sql
    assert "'whiteboard.entry', 'it''s', 1, '2026-11-22T10:00:00.123Z', 4," in sql
    assert "decode('000102', 'hex'), decode('ffef', 'hex'), 3, now()" in sql
    assert '"ref":"it\'\'s"' in sql
    org_level = forged(scope="org", scope_id=ORG_ID, wg="", task=WG_ID)
    sql = call_sql(ctx, node, {"event": org_level, "author_kind": "device"})
    assert f"'{ORG_ID}', h.seq, '{EVENT_ID}', 'org', '{ORG_ID}', NULL, '{WG_ID}', NULL, '{AUTHOR}', 'device'" in sql


def call_sql(ctx: Context, node: PsqlNode, body: Any) -> str:
    assert call(ctx, "POST", "/hub-db/forge-event", body)[0] == 200
    return node.runs[-1][0][-1]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (None, 'body must be {"event": {"header", "ct", "sig"}, "author_kind": "user"|"device"}'),
        ({"event": {"header": []}}, 'body must be {"event": {"header", "ct", "sig"}, "author_kind": "user"|"device"}'),
        ({"event": forged(), "author_kind": "hub"}, "author_kind must be user or device"),
        ({"event": forged(id="x")}, "header.id must be a UUID"),
        ({"event": forged(task=7)}, "header.task must be a UUID"),
        ({"event": forged(scope="cred")}, "header.scope must be org or wg"),
        ({"event": forged(kind="Bad kind")}, "header.kind must be an event kind"),
        ({"event": forged(kind=3)}, "header.kind must be an event kind"),
        ({"event": forged(ts="yesterday")}, "header.ts must be a UTC time"),
        ({"event": forged(ref=None)}, "header.ref must be text"),
        ({"event": forged(ref="x" * 201)}, "header.ref must be text"),
        ({"event": forged(epoch=True)}, "header.epoch must be a non-negative integer"),
        ({"event": forged(lh=-1)}, "header.lh must be a non-negative integer"),
        ({"event": {**forged(), "ct": "a+b"}}, "event.ct must be base64url"),
        ({"event": {**forged(), "sig": None}}, "event.sig must be base64url"),
        ({"event": {**forged(), "ct": "A"}}, "event.ct must be base64url"),
    ],
)
def test_forge_event_refusals(ctx: Context, body: Any, detail: str) -> None:
    assert call(ctx, "POST", "/hub-db/forge-event", body) == (400, {"error": "invalid", "detail": detail})


def test_spawn_with_the_operators_subscription_pool(ctx: Context) -> None:
    """KOR-49 (E2E-59): a platform runner with an operator-subscription directory (one token file, 0700/0600); the
    directory is removed with the runner; organization devices and non-boolean options are refused."""
    node = node_of(ctx)
    assert call(ctx, "POST", "/runners/runner-2/spawn", {"operator_subscriptions": False})[0] == 200
    assert "AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR" not in node.spawned["runner-2"]
    assert call(ctx, "POST", "/runners/runner-3/spawn", {"operator_subscriptions": True})[0] == 200
    directory = node.spawned["runner-3"]["AGENT_RUNNER_OPERATOR_SUBSCRIPTION_DIR"]
    assert directory == "/scratch/runner-3-operator-subscriptions"
    written = next(argv for argv, _env in node.runs if argv[:2] == ["/bin/sh", "-c"])
    assert written[-1] == directory
    assert call(ctx, "POST", "/runners/runner-3/remove")[0] == 200
    assert node.runs[-1][0] == ["rm", "-rf", directory]
    refused = call(ctx, "POST", "/runners/runner-4/spawn", {"operator_subscriptions": 1})
    assert refused == (400, {"error": "invalid", "detail": "operator_subscriptions must be a boolean"})
    org = call(ctx, "POST", "/runners/runner-4/spawn", {"operator_subscriptions": True, "device": "org"})
    assert org == (400, {"error": "invalid", "detail": "operator subscriptions are for platform devices only"})


def test_a_failing_operator_pool_is_reported(ctx: Context) -> None:
    node = node_of(ctx)
    original = node.call

    def failing(op: str, /, **args: Any) -> Any:
        if op == "run" and args["argv"][:2] == ["/bin/sh", "-c"]:
            return {"rc": 1, "stdout": "", "stderr": "mkdir: denied\n"}
        return original(op, **args)

    node.call = failing  # type: ignore[method-assign]
    status, answer = call(ctx, "POST", "/runners/runner-2/spawn", {"operator_subscriptions": True})
    assert (status, answer["detail"]) == (500, "operator subscriptions not written: mkdir: denied")
