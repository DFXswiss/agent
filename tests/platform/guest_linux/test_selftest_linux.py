"""Sandbox self-test on a real guest (05-runner.md section 4.5): green on the correct configuration, red when a
layer is weakened on purpose."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_guest.sandbox import probe, selftest, unit
from agent_guest.sandbox.selftest import SelftestConfig, ThrowawaySessions

from .conftest import LAUNCHER, PROBE, SANDBOX_DIR, SRC, Guest

pytestmark = pytest.mark.linux


def config(guest: Guest, **changes: object) -> SelftestConfig:
    values: dict[str, object] = {
        "python": guest.python,
        "lib_dynload": guest.lib_dynload,
        "exec_paths": guest.exec_paths,
        "claude": guest.claude,
        "launcher": str(LAUNCHER),
        "probe": str(PROBE),
        "extra_ro": (str(SANDBOX_DIR),),
    }
    values.update(changes)
    return SelftestConfig(**values)  # type: ignore[arg-type]


def failed(result: dict[str, object]) -> set[str]:
    checks = result["checks"]
    assert isinstance(checks, list)
    return {check["check"] for check in checks if not check["ok"]}


def test_selftest_passes_on_the_correct_configuration(guest: Guest, capsys: pytest.CaptureFixture[str]) -> None:
    result = selftest.run_selftest(config(guest))
    with capsys.disabled():
        print("\nguest-ci: self-test " + json.dumps({k: result[k] for k in ("ok", "kernel", "landlock_abi")}))
    assert failed(result) == set(), json.dumps(result, indent=1)
    assert result["ok"] is True
    assert result["unit_exit"] == 0
    assert len(result["checks"]) == len(probe.EXPECTED)
    assert not list(Path("/srv/sessions").iterdir()), "throw-away sessions were not removed"


def test_selftest_fails_when_the_noexec_mapping_is_lifted(guest: Guest) -> None:
    """Without NoExecPaths=/ the loader maps system binaries again (Landlock does not check mmap)."""
    result = selftest.run_selftest(config(guest, extra_properties=("NoExecPaths=",)))
    assert result["ok"] is False
    assert "ld_so_system_binary" in failed(result)


def test_selftest_fails_when_paths_are_no_longer_hidden(guest: Guest) -> None:
    result = selftest.run_selftest(config(guest, extra_properties=("InaccessiblePaths=",)))
    assert result["ok"] is False
    assert "guest_secrets_masked" in failed(result)


def test_selftest_fails_when_other_sessions_become_visible(guest: Guest) -> None:
    result = selftest.run_selftest(config(guest, extra_properties=("TemporaryFileSystem=",)))
    assert result["ok"] is False
    assert {"stat_other_session", "read_other_session"} <= failed(result)
    # without the empty /run the unix sockets of the system are reachable again (resolver varlink: DNS, ADR-045)
    assert "unix_sockets" in failed(result)


def test_outer_launcher_rights_are_replaced_by_the_probe_layer(guest: Guest) -> None:
    """The probe re-applies the session ruleset (landlock.plan), so extra outer rights do not reach the checks."""
    weakened = config(
        guest,
        extra_exec=("/usr/bin/dash",),
        extra_connect=(8787,),
        exec_paths=(*guest.exec_paths, "/usr/bin/dash"),
    )
    assert selftest.run_selftest(weakened)["ok"] is True


def test_selftest_times_out_and_stops_its_unit(guest: Guest) -> None:
    result = selftest.run_selftest(config(guest, timeout=0.05))
    assert result["ok"] is False
    assert result["unit_exit"] == -1
    assert "timed out" in str(result["stderr"])
    units = subprocess.run(
        ["systemctl", "list-units", "--all", "agent-selftest-*"], capture_output=True, text=True, check=False
    )
    assert "agent-selftest-" not in units.stdout
    assert not list(Path("/srv/sessions").iterdir())


def test_selftest_fails_when_other_processes_become_visible(guest: Guest) -> None:
    result = selftest.run_selftest(config(guest, extra_properties=("ProtectProc=default",)))
    assert result["ok"] is False
    assert failed(result) == {"list_processes"}


def test_selftest_fails_when_the_probe_cannot_start(guest: Guest) -> None:
    result = selftest.run_selftest(config(guest, extra_ro=()))  # the probe outside /opt/agent-guest is unreadable
    assert result["ok"] is False
    assert result["unit_exit"] != 0
    assert "missing" in {check["outcome"] for check in result["checks"]}


def test_probe_outside_the_sandbox_reports_what_it_could_do(sessions: ThrowawaySessions, guest: Guest) -> None:
    """Without the unit the probe finds the gaps the unit closes; it runs measured, with only its exec layer."""
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    code = "import sys; from agent_guest.sandbox import probe; sys.exit(probe.main(sys.argv[1:]))"
    session = ["--session-dir", f"/srv/sessions/{sessions.own}", "--exec", guest.claude, "--connect", "7700"]
    session += ["--rw", str(Path.cwd())]  # coverage data of this measured run
    result = subprocess.run(
        [sys.executable, "-c", code, sessions.own, sessions.other, guest.claude, "--", *session],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    seen = {item["check"]: item["outcome"] for item in map(json.loads, result.stdout.splitlines())}
    assert seen["layer2"] == "ok"
    # The session ruleset alone (Landlock) refuses exec, writes and connects even for root ...
    assert seen["exec_sh"] == "errno:EACCES"
    assert seen["write_etc"] == "errno:EACCES"
    assert seen["tcp_other_local_port"] == "errno:EACCES"
    # ... while what the unit hides is visible without it.
    assert seen["list_processes"].startswith("foreign:")
    assert seen["stat_other_session"] == "mode:755"
    assert seen["guest_secrets_masked"] == "mode:700"
    assert seen["unshare_user_namespace"] == "ok"


def test_probe_reports_a_failed_exec_layer(sessions: ThrowawaySessions) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    code = "import sys; from agent_guest.sandbox import probe; sys.exit(probe.main(sys.argv[1:]))"
    result = subprocess.run(
        [sys.executable, "-c", code, sessions.own, sessions.other, "/nonexistent/claude", "--"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert lines[0] == {"check": "layer2", "outcome": "errno:ENOENT"}


def test_throwaway_sessions_remove_is_safe_before_create() -> None:
    ThrowawaySessions().remove()


def test_selftest_command_line(guest: Guest, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = sorted(guest.exec_paths)
    (tmp_path / "exec-paths.json").write_text(json.dumps(paths))
    (tmp_path / "build.json").write_text(json.dumps({"exec_paths_sha256": unit.exec_paths_sha256(paths)}))
    seen: list[SelftestConfig] = []

    def run(cfg: SelftestConfig) -> dict[str, object]:
        seen.append(cfg)
        return selftest.run_selftest(config(guest, exec_paths=cfg.exec_paths, python=cfg.python))

    argv = ["--exec-paths-file", str(tmp_path / "exec-paths.json"), "--build-file", str(tmp_path / "build.json")]
    assert selftest.main(argv, run=run) == 0
    assert seen[0].python == guest.python
    assert seen[0].exec_paths == tuple(paths)
    assert json.loads(capsys.readouterr().out)["ok"] is True
