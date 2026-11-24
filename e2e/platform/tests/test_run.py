"""Whole runs against the fakes: T1 and T2 iterations, every failure path of setup, checks and teardown."""

from __future__ import annotations

import base64
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import pytest
from orchestrator import prechecks, run, services
from orchestrator.config import RunConfig, build_config
from orchestrator.node import LocalNode, Node, RemoteNode
from orchestrator.run import Run, Tools
from support import CORE, SUITE, World


def config_of(world: World, topology: str = "t1", **options: Any) -> RunConfig:
    return build_config(
        topology=topology,
        environ=world.environ,
        core_src=CORE,
        artifacts=str(world.artifacts),
        runner="tester@127.0.0.1" if topology == "t2" else None,
        **options,
    )


def tools_of(world: World, **overrides: Any) -> Tools:
    return Tools(hub_python=str(world.python), pg_bin=str(world.bin), playwright=str(world.playwright), **overrides)


def summary(world: World, iteration: int = 1) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((world.artifacts / f"iter-{iteration}" / "summary.json").read_text())
    return data


def git_answers(monkeypatch: pytest.MonkeyPatch, rc: int) -> None:
    """`git rev-parse HEAD` of the checkout answers `rc` whether or not the tests run in a git checkout (the copy of
    tools/wp/remote.sh has no .git)."""
    real_run = LocalNode.run

    def git(self: Node, argv: list[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
        if argv[:1] == ["git"]:
            return {"rc": rc, "stdout": "0" * 40 if rc == 0 else "", "stderr": "" if rc == 0 else "not a repository"}
        return real_run(self, argv, *args, **kwargs)

    monkeypatch.setattr(LocalNode, "run", git)


def test_t1_run_is_green_and_leaves_nothing(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    git_answers(monkeypatch, 0)  # a git checkout: the web build takes its commit from git itself
    world.set(pw_model="1", hub_commands="audit verify")
    assert Run(config_of(world, repeat=2), world.environ, tools_of(world)).execute() == run.EXIT_OK
    out = capsys.readouterr().out
    assert "all 2 iteration(s) green" in out
    assert "iteration 2/2: green" in out
    for number in (1, 2):
        root = world.artifacts / f"iter-{number}"
        assert summary(world, number)["ok"] is True
        assert (root / "hub-device" / "hub-db.sql").read_text().startswith("-- dump")
        assert {p.name for p in (root / "hub-device" / "logs").iterdir()} >= {"hub.log", "postgres.log", "runner-1.log"}
        egress = json.loads((root / "egress-summary.json").read_text())
        assert egress["violations"] == []
        assert egress["connections_per_link"]["runner-hub"] >= 1
        state = json.loads((root / "playwright" / "results.json").read_text())["state"]
        assert state["runners"] == {"runner-1": "waiting_for_enrollment"}
        assert sorted(state["links"]) == ["browser-hub", "github", "model", "runner-hub"]
    assert not (world.artifacts.parent / "artifacts-scratch" / "iter-1").exists()
    logged = world.logged()
    assert any(line.startswith("python -m agent_hub audit verify") for line in logged)
    assert any("node BUILD_COMMIT= KEY=4es19ejjw83HvBQaTcmCqzLKLe7OQffXqDUsvsMeYkE" in line for line in logged)
    playwright = next(line for line in logged if line.startswith("playwright "))
    assert "--project desktop --grep-invert @live" in playwright
    # The model double knows the run's platform key (the runners' key file) from its environment, never from argv.
    model_keys = {line for line in logged if line.startswith("model-key ")}
    runner_keys = {line.split()[1] for line in logged if line.startswith("runner-key ")}
    assert len(runner_keys) == 2  # a fresh key per iteration
    assert {line.split()[1] for line in model_keys} == runner_keys
    assert all(line.endswith("in-argv=False") for line in model_keys)
    assert playwright.endswith("cwd=web-coverage")


def test_build_without_git_metadata_gets_a_placeholder_commit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    git_answers(monkeypatch, 128)
    world.set(node_fail="1")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any(line.startswith("node BUILD_COMMIT=" + "0" * 40) for line in world.logged())


def test_the_release_like_web_build_is_minified(world: World) -> None:
    world.set(node_fail="1")
    environ = dict(world.environ, E2E_WEB_MINIFY="1")
    config = build_config(topology="t1", environ=environ, core_src=CORE, artifacts=str(world.artifacts))
    assert Run(config, environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any(line.startswith("node ") and line.endswith("MINIFY=1") for line in world.logged())
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any(line.startswith("node ") and line.endswith("MINIFY=0") for line in world.logged())


@pytest.mark.parametrize(
    ("switches", "expected"),
    [
        ({"pw_rc": "1"}, "Playwright exited with 1"),
        ({"pw_model": "1", "double_not_ok": "1"}, "model double: state not ok"),
        ({"pw_idle_request": "1"}, "model double: unexpected requests while idle"),
        ({"github_not_ok": "1"}, "github double: state not ok"),
        ({"runner_die": "1", "pw_sleep": "1"}, "runner-1 is not running any more"),
        ({"hub_commands": "audit verify", "audit_rc": "1"}, "hub audit chain does not verify: audit chain broken"),
        ({"hub_leak": "1"}, "secret in artefacts"),
        ({"dump_fail": "1"}, "teardown (hub database dump): pg_dump failed"),
        ({"initdb_fail": "1"}, "initdb failed: initdb: broken"),
        ({"psql_fail": "1"}, "psql failed: psql: broken"),
        ({"migrate_fail": "1"}, "agent-hub migrate failed: migration refused"),
        ({"hub_exit": "1"}, "hub exited"),
        ({"hub_healthy_calls": "1"}, "runner device cannot reach the hub through the runner relay (503)"),
        ({"double_exit": "1"}, "double-model exited with 1"),
        ({"double_state_status": "500"}, "model double: state answered 500"),
    ],
)
def test_iteration_failures(world: World, switches: dict[str, str], expected: str) -> None:
    world.set(**switches)
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    failures = summary(world)["failures"]
    assert any(expected in failure for failure in failures), failures


def test_unhealthy_hub_times_out(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "READY_TIMEOUT_S", 1.0)
    world.set(hub_health="503")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("hub not healthy within 1 s" in failure for failure in summary(world)["failures"])


def test_left_over_vms_fail_the_iteration(world: World) -> None:
    world.lima.write_text(json.dumps({"name": "wp-test-e2e-r1-pool-1", "status": "Running", "memory": 0}) + "\n")
    world.set(lima_sticky="1")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("VMs left after cleanup: wp-test-e2e-r1-pool-1" in f for f in summary(world)["failures"])
    assert "delete wp-test-e2e-r1-pool-1" in world.logged()


def test_node_prechecks_stop_the_iteration(world: World) -> None:
    (world.agent / "contracts" / "VERSION").write_text("0" * 64 + "\n")
    with socket.socket() as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", config_of(world).port("control")))
        busy.listen()
        environ = dict(world.environ, E2E_POOL_SIZE="1", E2E_VM_MEMORY_BUDGET_MIB="1")
        config = build_config(topology="t1", environ=environ, core_src=CORE, artifacts=str(world.artifacts))
        assert Run(config, environ, tools_of(world)).execute() == run.EXIT_FAILED
    failures = " | ".join(summary(world)["failures"])
    assert "pre-check CON-04 failed" in failures
    assert "pre-check ports failed" in failures
    assert "pre-check vm-budget failed" in failures


def test_prepare_failures(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(prechecks, "trace", lambda config: prechecks.Problem("traceability", "broken matrix"))
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_PRECHECK
    assert "pre-check traceability failed: broken matrix" in capsys.readouterr().out
    monkeypatch.undo()
    monkeypatch.setenv("HOME", world.environ["HOME"])
    world.set(node_fail="1")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert "FAILED before the run: web build failed: build: broken" in capsys.readouterr().out
    (world.agent / "pyproject.toml").write_text("changed\n")
    world.set(uv_fail="1")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert "agent checkout not ready: uv: broken" in capsys.readouterr().out


def test_vm_scenarios_ensure_the_golden_image(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "needs_vm", lambda suite, only: True)
    monkeypatch.setattr(run, "needs_workvm", lambda suite, only: False)
    (world.agent / "images").mkdir()
    (world.agent / "images" / "golden.yaml").write_text("images: []\n")
    (world.agent / "src" / "agent_guest").mkdir(parents=True)
    world.set(image="1")
    monkeypatch.setenv("PYTHONPATH", str(SUITE))
    # the cache is missing: built inside the run, which the run refuses (its downloads are not egress-sampled)
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("was built inside the run" in f for f in summary(world)["failures"])
    # now it exists: used as is
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_OK
    assert any(note.startswith("golden image cache-golden-") for note in summary(world)["notes"])
    world.clear()
    world.lima.write_text("")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("golden image: golden: agent-runner image build" in f for f in summary(world)["failures"])


def test_work_vm_scenarios_ensure_the_work_image(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "needs_vm", lambda suite, only: True)
    monkeypatch.setattr(run, "needs_workvm", lambda suite, only: True)
    inputs = ("images/golden.yaml", "src/agent_guest/x.py", "images/workvm.yaml", "images/workvm/provision.sh")
    for rel in (*inputs, "images/versions.json"):
        (world.agent / rel).parent.mkdir(parents=True, exist_ok=True)
        (world.agent / rel).write_text(rel)
    world.set(image="1")
    monkeypatch.setenv("PYTHONPATH", str(SUITE))
    # the golden cache first (built inside the run: refused; the golden hash covers images/, work image files too)
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("golden image cache-golden-" in f for f in summary(world)["failures"])
    # then the work image cache is missing: built inside the run, which the run refuses as well
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("work image cache-wimg-" in f and "was built inside the run" in f for f in summary(world)["failures"])
    # now it exists: used as is
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_OK
    assert any(note.startswith("work image cache-wimg-") for note in summary(world)["notes"])
    world.lima.write_text(world.lima.read_text().split("\n", 1)[0] + "\n")  # the golden cache stays
    world.set(workvm_build_fail="1")
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert any("work image: workvm image: work image build failed" in f for f in summary(world)["failures"])


def test_spec_tags_select_the_work_image(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.spec.ts").write_text("test('@E2E-48 @needs-vm @needs-workvm work', () => {});\n")
    assert run.needs_workvm(tmp_path, ()) is True
    assert run.needs_workvm(tmp_path, ("E2E-48",)) is True
    assert run.needs_workvm(tmp_path, ("E2E-40",)) is False


def remote_factory(world: World, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, list[str], IO[bytes] | None], Node]:
    """The runner device of T2 as a local `python -m orchestrator.node` (instead of SSH)."""
    monkeypatch.setenv("PYTHONPATH", str(SUITE))
    seen: list[list[str]] = []

    def make(name: str, argv: list[str], stderr: IO[bytes] | None) -> Node:
        seen.append(argv)
        run_dir = argv[-1].split("--run-dir ", 1)[1]
        return RemoteNode(name, [sys.executable, "-m", "orchestrator.node", "--run-dir", run_dir], stderr)

    make.seen = seen  # type: ignore[attr-defined]
    return make


def test_t2_runs_the_runner_side_over_a_node_connection(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.set(pw_model="1")
    commands: list[list[str]] = []

    def ssh(argv: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "venv: up to date\n", "")

    make = remote_factory(world, monkeypatch)
    tools = tools_of(world, remote=make, ssh=ssh, lan_address=lambda host: "127.0.0.1")
    assert Run(config_of(world, "t2"), world.environ, tools).execute() == run.EXIT_OK
    root = world.artifacts / "iter-1"
    assert (root / "runner-device" / "logs" / "runner-1.log").exists()
    assert (root / "runner-device" / "egress.jsonl").exists()
    assert (root / "runner-device-ssh.log").exists()
    assert summary(world)["ok"] is True
    assert commands[0][-2] == "tester@127.0.0.1"
    assert "/bin/sh -c" in commands[0][-1]
    node_argv = make.seen[0]  # type: ignore[attr-defined]
    assert node_argv[0] == "ssh"
    assert "-m orchestrator.node --run-dir" in node_argv[-1]
    assert f"UserKnownHostsFile={world.artifacts.with_name('artifacts-known_hosts')}" in node_argv


def test_t2_checkout_failure(world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def ssh(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "uv: missing\n")

    tools = tools_of(world, remote=remote_factory(world, monkeypatch), ssh=ssh)
    assert Run(config_of(world, "t2"), world.environ, tools).execute() == run.EXIT_FAILED
    assert "agent checkout on the runner device not ready: uv: missing" in capsys.readouterr().out
    world.set(pw_rc="0")
    ok = Tools(ssh=lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
    runner_run = Run(config_of(world, "t2"), world.environ, ok)
    runner_run.ensure_remote_checkout()
    assert "runner device: venv ready" in capsys.readouterr().out


def test_t2_resolves_a_runner_host_name(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.set(pw_model="1")
    make = remote_factory(world, monkeypatch)
    environ = dict(world.environ)
    config = build_config(
        topology="t2", environ=environ, core_src=CORE, artifacts=str(world.artifacts), runner="tester@localhost"
    )
    ssh = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    tools = tools_of(world, remote=make, ssh=ssh, lan_address=lambda host: "127.0.0.1")
    assert Run(config, environ, tools).execute() == run.EXIT_OK


def test_helpers(tmp_path: Path) -> None:
    suite = tmp_path / "e2e" / "platform"
    (suite / "specs").mkdir(parents=True)
    (suite / "specs" / "e2e-10-vm.spec.ts").write_text("test('@E2E-10 @needs-vm sandboxes', () => {});\n")
    (suite / "specs" / "e2e-11-x.spec.ts").write_text("test('@E2E-11 no VM', () => {});\n")
    assert run.needs_vm(suite, ()) is True
    assert run.needs_vm(suite, ("E2E-10",)) is True
    assert run.needs_vm(suite, ("E2E-11",)) is False
    run._link_node_modules(suite)
    assert os.readlink(suite / "node_modules") == "../../web/node_modules"
    (tmp_path / "web" / "node_modules").mkdir(parents=True)
    run._link_node_modules(suite)
    (suite / "node_modules").unlink()
    (suite / "node_modules").symlink_to("missing")
    run._link_node_modules(suite)
    assert os.readlink(suite / "node_modules") == "../../web/node_modules"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"hello"
        info = tarfile.TarInfo("logs/a.log")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    run._extract(base64.b64encode(buffer.getvalue()).decode(), tmp_path / "out")
    assert (tmp_path / "out" / "logs" / "a.log").read_bytes() == b"hello"
    assert run.lan_address("127.0.0.1") == "127.0.0.1"
    assert run._quote("a'b") == "'a'\"'\"'b'"
    assert run._is_ip("192.0.2.1")
    assert not run._is_ip("localhost")


def test_grep_arguments(world: World) -> None:
    assert run.grep_args(config_of(world)) == ["--project", "desktop", "--grep-invert", "@live"]
    config = config_of(world, only="E2E-06,E2E-08", live=True, project="mobile")
    assert run.grep_args(config) == ["--project", "mobile", "--grep", "@E2E-06(?![0-9])|@E2E-08(?![0-9])"]


def test_sigterm_ends_the_run_through_the_finally_blocks() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    try:
        run.install_sigterm()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(SystemExit) as stopped:
            handler(signal.SIGTERM, None)
        assert stopped.value.code == 143
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_default_tools_run_ssh() -> None:
    done = Tools().ssh(["true"])
    assert done.returncode == 0


def test_egress_violations_fail_the_iteration(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {"source": "sampler", "allowed": False, "remote": "198.51.100.9:443", "command": "chrome"}
    monkeypatch.setattr(run, "violations", lambda records: [dict(bad, device="hub-device")])
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert "egress to 198.51.100.9:443 by chrome on hub-device" in summary(world)["failures"]


def test_sampler_errors_and_a_lost_runner_device(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "violations", lambda records: [{"error": "lsof exited with 2", "device": "hub-device"}])
    assert Run(config_of(world), world.environ, tools_of(world)).execute() == run.EXIT_FAILED
    assert "egress sampler on hub-device: lsof exited with 2" in summary(world)["failures"]
    monkeypatch.undo()
    monkeypatch.setenv("HOME", world.environ["HOME"])

    def unreachable(name: str, argv: list[str], stderr: IO[bytes] | None) -> Node:
        raise RuntimeError("no route to the runner device")

    ssh = lambda argv: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    assert Run(config_of(world, "t2"), world.environ, tools_of(world, remote=unreachable, ssh=ssh)).execute() == 1
    assert summary(world)["failures"] == ["RuntimeError: no route to the runner device"]
