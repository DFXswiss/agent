"""Services edge cases, VMs, golden cache, pre-checks, artefact scan and the command line."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
from orchestrator import artifacts, cli, golden, prechecks, run, services, vms
from orchestrator.config import RunConfig
from orchestrator.device import Device
from orchestrator.node import LocalNode, NodeError
from support import CORE, World


def local(world: World) -> LocalNode:
    return LocalNode("dev", Device(world.root / "dev"))


def config(world: World, **changes: Any) -> RunConfig:
    base: dict[str, Any] = {
        "topology": "t1",
        "core_src": CORE,
        "agent_src": world.agent,
        "artifacts": world.artifacts,
        "port_base": 21000,
        "vm_prefix": "wp-t-",
    }
    base.update(changes)
    return RunConfig(**base)


# ---------------------------------------------------------------- services


def test_pg_bin_and_root_key(world: World, tmp_path: Path) -> None:
    node = local(world)
    assert services.find_pg_bin(node, ("/nonexistent", str(world.bin))) == str(world.bin)
    with pytest.raises(services.ServiceError, match="PostgreSQL 16 not found"):
        services.find_pg_bin(node, ("/nonexistent",))
    core = tmp_path / "core"
    (core / "web" / "build").mkdir(parents=True)
    (core / "web" / "build" / "manifest.ts").write_text("export const OTHER = 1;\n")
    with pytest.raises(services.ServiceError, match="names no TEST_ROOT_KEY"):
        services.test_root_key(core)
    secrets = services.Secrets()
    assert len(secrets.new(4)) == 8
    assert secrets.values


def test_runner_state_while_running_after_enrollment(world: World) -> None:
    node = local(world)
    runner = services.Runner(node, "r", str(world.python), {"HOME": str(world.home), "PATH": os.environ["PATH"]})
    runner.start()
    (node.device.logs / "r.log").write_text("runner.run tasks=2 enrolled=yes\n")
    assert runner.state() == "running"
    assert runner.stop() in (0, -15)
    assert runner.state() == "stopped"


def test_double_state_shapes(world: World) -> None:
    class Answer(LocalNode):
        def http(self, method: str, url: str, body: Any = None, timeout: float = 10.0) -> dict[str, Any]:
            return {"status": 200, "body": "text"}

    double = services.Double(Answer("dev", Device(world.root / "d")), "model", 1, 2)
    with pytest.raises(services.ServiceError, match="state answered 200"):
        double.state()


def test_hub_start_survives_refused_connections(world: World) -> None:
    calls = []

    class Flaky(LocalNode):
        def http(self, method: str, url: str, body: Any = None, timeout: float = 10.0) -> dict[str, Any]:
            calls.append(url)
            if len(calls) == 1:
                raise NodeError("refused")
            return {"status": 200, "body": {}}

        def alive(self, name: str) -> bool:
            return True

        def spawn(self, name: str, argv: list[str], env: dict[str, str], cwd: str | None = None) -> int:
            return 1

    hub = services.Hub(Flaky("dev", Device(world.root / "h")), "py", {}, "127.0.0.1", 1, sleep=lambda s: None)
    hub.start()
    assert len(calls) == 2


# ---------------------------------------------------------------- VMs


def lima(world: World, *entries: tuple[str, str, int]) -> None:
    world.lima.write_text(
        "".join(json.dumps({"name": n, "status": s, "memory": m * vms.MIB}) + "\n" for n, s, m in entries)
    )


def test_vms(world: World) -> None:
    node = local(world)
    lima(world, ("a", "Running", 4096), ("b", "Stopped", 8192), ("wp-t-e2e-r1-x", "Running", 1024))
    listed = vms.list_vms(node)
    assert listed is not None
    assert [v.name for v in listed] == ["a", "b", "wp-t-e2e-r1-x"]
    vms.check_budget(listed, 1000, 6120)
    with pytest.raises(vms.VmError, match="5120 MiB running \\+ 1000 MiB planned > 6119"):
        vms.check_budget(listed, 1000, 6119)
    assert vms.cleanup(node, "wp-t-e2e-") == ["wp-t-e2e-r1-x"]
    world.set(lima_fail="1")
    with pytest.raises(vms.VmError, match="limactl list failed: limactl: broken"):
        vms.list_vms(node)
    world.clear()
    world.set(memsize="x")
    with pytest.raises(vms.VmError, match="device memory unknown"):
        vms.default_budget(node)
    world.clear()
    assert vms.default_budget(node, reserve_mib=1024) == 65536 - 1024
    assert vms.parse_list("\n") == []
    world.set(lima_missing="1")
    assert vms.list_vms(node) is None
    assert vms.cleanup(node, "x") == []


# ---------------------------------------------------------------- golden


def test_golden(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    with pytest.raises(golden.GoldenError, match="golden inputs missing"):
        golden.tree_sha(agent)
    for rel, text in {
        "images/golden.yaml": "a",
        "src/agent_guest/x.py": "b",
        "src/agent_guest/__pycache__/x.pyc": "c",
        "images/.DS_Store": "d",
        "contracts/VERSION": "e",
    }.items():
        (agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (agent / rel).write_text(text)
    lines = "".join(
        f"{hashlib.sha256(t.encode()).hexdigest()}  {r}\n"
        for r, t in sorted({"contracts/VERSION": "e", "images/golden.yaml": "a", "src/agent_guest/x.py": "b"}.items())
    )
    assert golden.tree_sha(agent) == hashlib.sha256(lines.encode()).hexdigest()
    name = golden.cache_name(agent)
    listing = {"names": ""}
    results: dict[str, int] = {}
    calls: list[list[str]] = []
    envs: list[dict[str, str] | None] = []

    def fake(argv: list[str], env: dict[str, str] | None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        envs.append(env)
        if argv[:2] == ["limactl", "list"]:
            return subprocess.CompletedProcess(argv, 0, listing["names"], "")
        rc = results.get(argv[4], 0)
        if argv[4] == "build" and rc == 0:
            listing["names"] = name + "\n"
        return subprocess.CompletedProcess(argv, rc, "", "build log")

    locks = tmp_path / "locks"
    assert golden.ensure(agent, "py", tmp_path / "h", locks, run=fake) == name
    assert golden.ensure(agent, "py", tmp_path / "h", locks, run=fake) == name
    assert sum(1 for c in calls if "build" in c) == 1
    (build,) = [c for c in calls if "build" in c]
    assert build[-4:] == ["--tag", name.rsplit("-", 1)[1], "--test-model-port", "9"]
    (build_env,) = [e for c, e in zip(calls, envs, strict=True) if "build" in c]
    assert build_env is not None
    assert build_env["AGENT_RUNNER_TEST_MODE"] == "1"
    assert not list(locks.iterdir())
    listing["names"] = ""
    results["build"] = 1
    assert (
        golden.main(
            ["--agent", str(agent), "--python", "py", "--home", str(tmp_path), "--lock-dir", str(locks)], run=fake
        )
        == 1
    )
    results["--help"] = 2
    assert (
        golden.main(
            ["--agent", str(agent), "--python", "py", "--home", str(tmp_path), "--lock-dir", str(locks)], run=fake
        )
        == 3
    )
    results.clear()
    assert (
        golden.main(
            ["--agent", str(agent), "--python", "py", "--home", str(tmp_path), "--lock-dir", str(locks)], run=fake
        )
        == 0
    )
    assert golden._run(["true"], None).returncode == 0


def test_golden_lock(tmp_path: Path) -> None:
    lock = tmp_path / "l.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999")
    golden.acquire(lock, alive=lambda pid: False)
    assert (lock / "pid").read_text() == str(os.getpid())
    waits: list[float] = []
    (lock / "pid").write_text("1")

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        (lock / "pid").write_text("2")

    golden.acquire(lock, alive=lambda pid: pid == 1, sleep=sleep)
    assert waits == [golden.LOCK_WAIT_S]
    assert golden.pid_alive(os.getpid())
    assert not golden.pid_alive(999999)
    assert golden.pid_alive(1)


# ---------------------------------------------------------------- pre-checks


def test_prechecks(world: World, tmp_path: Path) -> None:
    node = local(world)
    assert prechecks.contracts_version(node, str(CORE), node, str(world.agent)) is None
    missing = prechecks.contracts_version(node, str(CORE), node, str(tmp_path))
    assert missing is not None
    assert missing.check == "CON-04"
    assert "is missing" in missing.detail
    assert str(missing).startswith("pre-check CON-04 failed:")
    assert prechecks.trace(config(world)) is None
    bad_suite = tmp_path / "core" / "e2e" / "platform"
    bad_suite.mkdir(parents=True)
    (bad_suite / "traceability.json").write_text("{")
    problem = prechecks.trace(config(world, core_src=tmp_path / "core"))
    assert problem is not None
    assert problem.check == "traceability"
    world.set(lima_missing="1")
    assert prechecks.budget(node, config(world)) is None
    no_lima = prechecks.budget(node, config(world, pool_size=1))
    assert no_lima is not None
    assert "Lima is not installed" in no_lima.detail


def test_budget_uses_the_default(world: World) -> None:
    node = local(world)
    assert prechecks.budget(node, config(world, pool_size=1), default=lambda n: 100000) is None
    assert prechecks.budget(node, config(world, pool_size=1), default=lambda n: 10) is not None


# ---------------------------------------------------------------- artefacts


def test_artifact_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    root.mkdir()
    (root / "clean.log").write_text("nothing here")
    (root / "leak.log").write_text("key=" + "f" * 64)
    (root / "token.txt").write_text("gh" + "p_" + "A" * 36)
    (root / "video.webm").write_bytes(b"gh" + b"p_" + b"A" * 36)
    with zipfile.ZipFile(root / "trace.zip", "w") as archive:
        archive.writestr("dir/", "")
        archive.writestr("net.json", "-----BEGIN " + "PRIVATE KEY-----")
    (root / "broken.zip").write_text("not a zip")
    findings = artifacts.scan(root, ["f" * 64, "short"])
    assert f"{root}/leak.log: generated run secret" in findings
    assert f"{root}/token.txt: github token" in findings
    assert f"{root}/trace.zip!net.json: private key" in findings
    assert f"{root}/broken.zip: unreadable" in findings
    assert not any("video" in f or "clean" in f for f in findings)
    monkeypatch.setattr(artifacts, "MAX_BYTES", 1)
    assert artifacts.scan(root, ["f" * 64]) == [f"{root}/broken.zip: unreadable"]


# ---------------------------------------------------------------- command line


def test_cli(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--topology", "t2"], world.environ) == cli.EXIT_USAGE
    assert "--runner <ssh target> is required" in capsys.readouterr().err
    seen: list[RunConfig] = []

    def execute(self: run.Run) -> int:
        seen.append(self.config)
        return 0

    monkeypatch.setattr(run.Run, "execute", execute)
    monkeypatch.setattr(cli, "install_sigterm", lambda: None)
    environ = dict(world.environ, AGENT_CORE_SRC=str(CORE))
    assert (
        cli.main(
            ["--topology", "t1", "--only", "E2E-00", "--repeat", "2", "--artifacts", str(world.artifacts)], environ
        )
        == 0
    )
    assert seen[0].only == ("E2E-00",)
    assert seen[0].repeat == 2
    assert seen[0].core_src == CORE
    monkeypatch.setattr(os, "environ", world.environ)
    assert cli.main(["--topology", "t1"]) == 0
    assert seen[1].core_src == cli.CORE_SRC
    assert importlib.import_module("orchestrator.__main__").main is cli.main
