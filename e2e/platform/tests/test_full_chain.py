"""The orchestrator parts of the full-chain scenarios (SCN-01): enrollment, pool status and operator routes, the
operator and platform key files, the golden adoption and the guest tunnels of the model double (ADR-034)."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from orchestrator import services, tunnels
from orchestrator.config import RunConfig
from orchestrator.context import Context
from orchestrator.device import Device
from orchestrator.node import LocalNode
from orchestrator.routes import pool
from orchestrator.run import Run
from orchestrator.services import Hub, Runner, ServiceError
from support import CORE, World
from test_routes import ROUTES, StubNode, call, ctx  # noqa: F401 - the fixture is used by name

ROUTES_USED = ROUTES


class ChainNode(StubNode):
    """`StubNode` plus relays that can be opened and runner commands keyed by the subcommand."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: dict[str, Any] = {}

    def call(self, op: str, /, **args: Any) -> Any:
        if op == "relays":
            self.calls.append((op, args))
            return dict(self.opened)
        if op == "relay_start":
            self.calls.append((op, args))
            self.opened[args["name"]] = {"listen": args["listen"], "target": args["target"]}
            return {"cut": False}
        return super().call(op, **args)


def runs(node: StubNode, prefix: str) -> list[list[str]]:
    return [args["argv"] for op, args in node.calls if op == "run" and args["argv"][3:4] == [prefix]]


def test_enroll_restarts_the_runner_around_the_enrollment(ctx: Context) -> None:  # noqa: F811
    node = ctx.runner_node
    assert isinstance(node, StubNode)
    assert call(ctx, "POST", "/runners/runner-9/enroll", {"code": "c"})[0] == 404
    for body in (None, {"code": ""}, {"code": "c", "platform": "yes"}, {"platform": True}):
        assert call(ctx, "POST", "/runners/runner-1/enroll", body)[0] == 400
    node.runs["enroll --hub"] = {"rc": 0, "stdout": "runner.enroll state=pending device_id=d\n", "stderr": ""}
    status, answer = call(ctx, "POST", "/runners/runner-1/enroll", {"code": "ABCD-EFGH", "platform": True})
    assert status == 200
    assert answer == {
        "rc": 0,
        "stdout": "runner.enroll state=pending device_id=d\n",
        "stderr": "",
        "state": "waiting_for_enrollment",
    }
    ops = [op for op, _ in node.calls if op in ("stop", "run", "spawn")]
    assert ops == ["stop", "run", "spawn"]
    (argv,) = runs(node, "enroll")
    assert argv == [
        "/agent/python",
        "-m",
        "agent_runner",
        "enroll",
        "--hub",
        ctx.config.public_origin,
        "--code",
        "ABCD-EFGH",
        "--platform",
    ]
    # A stopped runner is not stopped again; an org enrollment has no --platform.
    ctx.runners["runner-1"].stop()
    node.calls.clear()
    node.runs["enroll --hub"] = {"rc": 1, "stdout": "", "stderr": "runner.error code=invite_unknown\n"}
    status, answer = call(ctx, "POST", "/runners/runner-1/enroll", {"code": "X"})
    assert (status, answer["rc"]) == (200, 1)
    assert [op for op, _ in node.calls if op in ("stop", "run", "spawn")] == ["run", "spawn"]
    assert "--platform" not in runs(node, "enroll")[0]


def test_pool_status(ctx: Context) -> None:  # noqa: F811
    node = ctx.runner_node
    assert isinstance(node, StubNode)
    node.runs["pool status"] = {
        "rc": 0,
        "stdout": "runner.pool.vm vm_id=v\nrunner.pool size=1 ready=0 golden=g refill=pending\n",
        "stderr": "",
    }
    assert call(ctx, "GET", "/pool") == (200, {"runner-1": {"size": 1, "ready": 0, "golden": "g", "refill": "pending"}})
    node.runs["pool status"] = {"rc": 1, "stdout": "", "stderr": "runner.error code=store_unavailable"}
    status, answer = call(ctx, "GET", "/pool")
    assert (status, answer["error"]) == (500, "failed")
    assert "runner-1: pool status exited with 1" in answer["detail"]
    node.runs["pool status"] = {"rc": 0, "stdout": "runner.capacity slots_free=1\n", "stderr": ""}
    assert call(ctx, "GET", "/pool")[1] == {"error": "failed", "detail": "ValueError: no runner.pool line"}


def test_pool_drain_deletes_the_reported_pool_vms_and_sets_the_size(
    ctx: Context,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = ctx.runner_node
    assert isinstance(node, StubNode)
    runner = ctx.runners["runner-1"]
    runner.env["AGENT_RUNNER_VM_PREFIX"] = "wp-t-e2e-r1-"
    # The runner's pool is fixed at 1 (ADR-009 addendum): 0 holds it empty, 1 is its own pool, nothing else.
    for body in ({"size": -1}, {"size": 2}, {"size": 9}, {"size": True}, ["x"]):
        assert call(ctx, "POST", "/pool/drain", body)[0] == 400
    listing = "runner.pool.vm vm_id=vm-1 lima_name=wp-t-e2e-r1-arp-1 golden_version=g created_at=t instance=Running\n"
    listing += "runner.pool.vm vm_id=vm-2 lima_name=foreign-arp-2 golden_version=g created_at=t instance=Running\n"
    before = {"rc": 0, "stdout": listing + "runner.pool size=1 ready=2 golden=g refill=full\n", "stderr": ""}
    node.runs["pool status"] = before
    # The restarted daemon's reconciliation removes the deleted rows; the foreign VM is not a pool VM of the run.
    after = {"rc": 0, "stdout": "runner.pool size=0 ready=0 golden=g refill=disabled\n", "stderr": ""}
    monkeypatch.setattr(pool, "pause", lambda _seconds: node.runs.update({"pool status": after}))
    assert call(ctx, "POST", "/pool/drain") == (200, {"runner-1": {"deleted": ["wp-t-e2e-r1-arp-1"], "size": 0}})
    node.runs["pool status"] = before
    deletes = [args["argv"] for op, args in node.calls if op == "run" and args["argv"][0] == "limactl"]
    assert deletes == [["limactl", "delete", "-f", "wp-t-e2e-r1-arp-1"]]
    assert runner.env["AGENT_RUNNER_TEST_POOL_EMPTY"] == "1"
    assert "AGENT_RUNNER_POOL_SIZE" not in runner.env
    assert [op for op, _ in node.calls if op in ("stop", "spawn")] == ["stop", "spawn"]
    # A stopped runner stays stopped; the size is set for its next start.
    runner.stop()
    node.calls.clear()
    assert call(ctx, "POST", "/pool/drain", {"size": 1})[1] == {
        "runner-1": {"deleted": ["wp-t-e2e-r1-arp-1"], "size": 1}
    }
    assert [op for op, _ in node.calls if op in ("stop", "spawn")] == []
    assert "AGENT_RUNNER_TEST_POOL_EMPTY" not in runner.env
    node.runs["pool status"] = {"rc": 1, "stdout": "", "stderr": "x"}
    assert call(ctx, "POST", "/pool/drain", {"size": 1})[1]["detail"] == "runner-1: pool status exited with 1"


def test_pool_drain_waits_until_the_restarted_runner_no_longer_lists_the_deleted_vms(
    ctx: Context,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restarted daemon marks the rows of the deleted instances only in its start-up reconciliation (after the
    `enrolled=` line): the route answers once `pool status` no longer lists them and holds at most `size` VMs, so a
    spec never reads a stale ready VM (E2E-07 in the whole suite); it fails when the pool does not settle."""
    node = ctx.runner_node
    assert isinstance(node, StubNode)
    ctx.runners["runner-1"].env["AGENT_RUNNER_VM_PREFIX"] = "wp-t-e2e-r1-"
    stale = "runner.pool.vm vm_id=vm-1 lima_name=wp-t-e2e-r1-arp-1 golden_version=g created_at=t instance=Running\n"
    node.runs["pool status"] = {
        "rc": 0,
        "stdout": stale + "runner.pool size=1 ready=1 golden=g refill=full\n",
        "stderr": "",
    }
    pauses: list[float] = []

    def settle(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 2:  # the reconciliation has run
            node.runs["pool status"] = {
                "rc": 0,
                "stdout": "runner.pool size=0 ready=0 golden=g refill=full\n",
                "stderr": "",
            }

    monkeypatch.setattr(pool, "pause", settle)
    assert call(ctx, "POST", "/pool/drain") == (200, {"runner-1": {"deleted": ["wp-t-e2e-r1-arp-1"], "size": 0}})
    assert pauses == [pool.SETTLE_POLL_S, pool.SETTLE_POLL_S]
    # A pool that keeps the deleted VM (or more VMs than the size) fails the route after the settle time.
    node.runs["pool status"] = {
        "rc": 0,
        "stdout": stale + "runner.pool size=1 ready=1 golden=g refill=full\n",
        "stderr": "",
    }
    clock = iter([0.0, 1.0, pool.SETTLE_TIMEOUT_S + 1.0])
    monkeypatch.setattr(pool, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pool, "pause", lambda _seconds: None)
    status, answer = call(ctx, "POST", "/pool/drain")
    assert (status, answer["error"]) == (500, "failed")
    assert answer["detail"] == "runner-1: pool did not settle at size 0 (still listed: wp-t-e2e-r1-arp-1)"


def test_operator_gets_the_key_file_and_in_t2_its_relay(ctx: Context, tmp_path: Path) -> None:  # noqa: F811
    hub_node = ChainNode()
    ctx.hub_node = hub_node
    ctx.hub = Hub(hub_node, "/hub/python", {"H": "1"}, "192.0.2.10", 21006)
    ctx.bind = "192.0.2.10"
    hub_node.runs["platform invite-device"] = {"rc": 0, "stdout": "invitation code X\n", "stderr": ""}
    assert call(ctx, "POST", "/operator", {"args": ["invite-device", "--expires-hours", "2"]})[1]["rc"] == 0
    argv = next(args["argv"] for op, args in hub_node.calls if op == "run" and "invite-device" in args["argv"])
    key = tmp_path / "platform-root.json"
    assert argv == [
        "/hub/python",
        "-m",
        "agent_hub",
        "platform",
        "invite-device",
        "--key-file",
        str(key),
        "--hub",
        ctx.config.public_origin,
        "--expires-hours",
        "2",
    ]
    assert hub_node.opened == {
        "operator-hub": {"listen": ["127.0.0.1", ctx.config.port("public")], "target": ["192.0.2.10", 21006]}
    }
    # The relay is opened once; the key file is kept.
    before = key.read_text()
    call(ctx, "POST", "/operator", {"args": ["certify-pending"]})
    assert [op for op, _ in hub_node.calls].count("relay_start") == 1
    assert key.read_text() == before


def test_operator_key_file_holds_the_public_test_seed(tmp_path: Path) -> None:
    path = services.write_operator_key(tmp_path / "root.json")
    data = json.loads(path.read_text())
    assert data == {"v": 1, "root": "h_llXWnzVLZMH0UBFb7uPFFu1k147QJdoXRdK_N_tas"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("kept\n")
    assert services.write_operator_key(path).read_text() == "kept\n"


def test_platform_key_file_and_adoption(world: World, tmp_path: Path) -> None:
    node = LocalNode("device", Device(tmp_path / "run"))
    target = tmp_path / "home" / "keys" / "platform.key"
    services.write_platform_key(node, str(target), "sk-run-secret")
    assert target.read_text() == "sk-run-secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(ServiceError, match="platform key file not written"):
        services.write_platform_key(node, "/nonexistent-root/x/key", "k")
    runner = Runner(node, "runner-1", str(world.python), {"HOME": str(world.home), "PATH": os.environ["PATH"]})
    runner.adopt("cache-golden-0123456789ab")
    world.set(adopt_fail="1")
    with pytest.raises(ServiceError, match=r"runner-1: image adopt failed: runner\.error code=golden_missing"):
        runner.adopt("cache-golden-0123456789ab")
    env = services.runner_env(
        {}, ctx_config(tmp_path), index=1, home="~/h", root_key="k", model_url="http://m", github_url="http://g"
    )
    assert "AGENT_RUNNER_OPENROUTER_KEY_FILE" not in env
    assert (env["AGENT_RUNNER_GITHUB_API_URL"], env["AGENT_RUNNER_GITHUB_WEB_URL"]) == ("http://g", "http://g")
    assert env["AGENT_RUNNER_GITHUB_CLIENT_ID"] == "Iv1.0000double0000"
    keyed = services.runner_env(
        {}, ctx_config(tmp_path), index=1, home="~/h", root_key="k", model_url="m", github_url="g", key_file="f"
    )
    assert keyed["AGENT_RUNNER_OPENROUTER_KEY_FILE"] == "f"


def ctx_config(tmp_path: Path) -> RunConfig:
    return RunConfig(
        topology="t1", core_src=CORE, agent_src=tmp_path, artifacts=tmp_path / "a", port_base=21000, vm_prefix="wp-t-"
    )


# ---------------------------------------------------------------------------------------------------- tunnels


class FakeProc:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.code: int | None = None
        self.terminated = False
        self.slow = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        if self.slow:
            raise subprocess.TimeoutExpired("ssh", timeout or 0)
        return 0


def listing(*items: tuple[str, str]) -> str:
    lines = [json.dumps({"name": name, "status": status, "dir": f"/l/{name}"}) for name, status in items]
    return "\n".join([*lines, "", json.dumps({"name": "wp-x-e2e-nodir", "status": "Running"})])


def test_tunnels_follow_the_running_vms() -> None:
    state = {"text": listing(("wp-x-e2e-r1-arp-1", "Running"), ("wp-x-e2e-r1-arp-2", "Stopped"), ("other", "Running"))}
    spawned: list[FakeProc] = []
    lines: list[str] = []

    def spawn(argv: list[str]) -> FakeProc:
        spawned.append(FakeProc(argv))
        return spawned[-1]

    now = [100.0]
    watcher = tunnels.Tunnels(
        "wp-x-e2e-", (20930, 20931), lister=lambda: state["text"], spawn=spawn, say=lines.append, clock=lambda: now[0]
    )
    watcher.step()
    watcher.step()
    assert len(spawned) == 1
    assert spawned[0].argv == [
        "ssh",
        "-F",
        "/l/wp-x-e2e-r1-arp-1/ssh.config",
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-R",
        "127.0.0.1:20930:127.0.0.1:20930",
        "-R",
        "127.0.0.1:20931:127.0.0.1:20931",
        "lima-wp-x-e2e-r1-arp-1",
    ]
    # The VM restarts: the old tunnel ended, a new one opens; the second VM runs now too.
    spawned[0].code = 255
    state["text"] = listing(("wp-x-e2e-r1-arp-1", "Running"), ("wp-x-e2e-r1-arp-2", "Running"))
    watcher.step()
    now[0] += tunnels.RETRY_S
    watcher.step()
    assert lines == [
        "tunnel open wp-x-e2e-r1-arp-1",
        "tunnel closed wp-x-e2e-r1-arp-1 rc=255",
        "tunnel open wp-x-e2e-r1-arp-2",
        "tunnel open wp-x-e2e-r1-arp-1",
    ]
    spawned[-1].slow = True
    watcher.close()
    assert watcher.open == {}
    assert all(proc.terminated for proc in spawned[1:])


def test_tunnels_main_ends_on_sigterm(capsys: pytest.CaptureFixture[str]) -> None:
    made: list[tunnels.Tunnels] = []
    steps: list[str] = []

    def make(prefix: str, ports: tuple[int, ...]) -> tunnels.Tunnels:
        made.append(tunnels.Tunnels(prefix, ports, lister=lambda: "", spawn=FakeProc, say=steps.append))
        return made[-1]

    def sleep(_seconds: float) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert tunnels.main(["--prefix", "wp-x-e2e-", "--port", "20930"], tunnels=make, sleep=sleep) == 0
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert made[0].ports == (20930,)
    assert capsys.readouterr().out == "tunnels watching wp-x-e2e-* ports 20930\n"
    with pytest.raises(SystemExit):
        tunnels.main(["--prefix", "nodash", "--port", "1"])


def test_tunnels_list_and_spawn(world: World) -> None:
    world.lima.write_text(json.dumps({"name": "wp-a", "status": "Running", "dir": "/d"}) + "\n")
    assert tunnels.running(tunnels.list_instances(), "wp-") == {"wp-a": "/d"}
    world.set(lima_fail="1")
    assert tunnels.list_instances() == ""
    proc = tunnels.spawn_ssh([sys.executable, "-c", "pass"])
    assert proc.wait(timeout=10) == 0


def test_runner_side_environment_in_t2(ctx: Context) -> None:  # noqa: F811
    env = {"HOME": "/hub/home", "TMPDIR": "/hub/tmp", "USER": "hub", "LOGNAME": "hub", "PATH": "/bin", "A": "1"}
    assert Run.runner_side(ctx, env) is env
    remote = ChainNode()
    remote.remote = True
    ctx.runner_node = remote
    assert Run.runner_side(ctx, env) == {"HOME": "~", "PATH": "/bin", "A": "1"}
