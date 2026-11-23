"""Sandbox self-test (docs/platform/spec/05-runner.md section 4.5, ADR-008); runs as root inside the guest.

Runs at golden build (VM-01), when a pool VM is filled (VM-02) and when the guest version changes - never on the
hot path. It creates a throw-away session (user in 20000-29999, directories, harness file) next to a second
"other" session, starts ``probe.py`` in a unit with exactly the properties of a session (``unit.systemd_run``)
under the session's Landlock rules, compares every observation with ``probe.EXPECTED`` and removes both sessions
again. A failed self-test means: the caller sets the guest health to ``state:"broken"`` and never assigns the VM.

Command line (prints the result JSON, exit 0 when it passed, 1 otherwise)::

    python3 -m agent_guest.sandbox.selftest [--exec-paths-file F] [--build-file F] [--python P]

Result JSON: ``{"ok", "checks": [{"check", "outcome", "expected", "ok"}], "kernel", "landlock_abi",
"unit_exit", "stderr"}``.

Interface:

* ``SelftestConfig`` - interpreter, exec paths and binaries; ``extra_exec``/``extra_connect``/``extra_properties``
  weaken the configuration on purpose (tests prove the self-test fails then; ``extra_exec`` only affects the outer
  launcher layer, which the probe's own exec-only layer replaces); ``extra_ro`` grants read access to a probe
  outside ``/opt/agent-guest`` (tests)
* ``run_selftest(config) -> dict`` - the complete self-test
* ``evaluate(lines: Iterable[str]) -> list[dict]`` - parse probe output and compare it with ``probe.EXPECTED``
* ``free_uid(used: Collection[int]) -> int`` - lowest free UID of the session range
* ``main(argv=None, *, run=run_selftest, out=None) -> int``
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from agent_guest.sandbox import landlock, probe, unit

UID_RANGE = range(20000, 30000)
PROBE = str(Path(unit.LAUNCHER).with_name("probe.py"))
UNIT_PREFIX = "agent-selftest-"
TIMEOUT = 180.0
STDERR_TAIL = 2000


class SelftestError(RuntimeError):
    """The self-test could not be set up (not a sandbox finding)."""


@dataclass(frozen=True)
class SelftestConfig:
    python: str
    lib_dynload: str
    exec_paths: Sequence[str]
    claude: str = unit.CLAUDE_BINARY
    launcher: str = unit.LAUNCHER
    probe: str = PROBE
    extra_ro: Sequence[str] = ()
    extra_exec: Sequence[str] = ()
    extra_connect: Sequence[int] = ()
    extra_properties: Sequence[str] = ()
    timeout: float = TIMEOUT


def free_uid(used: Collection[int]) -> int:
    """Lowest UID of 20000-29999 that is neither a user nor a group id."""
    for candidate in UID_RANGE:
        if candidate not in used:
            return candidate
    raise SelftestError("no free UID in 20000-29999")


def evaluate(lines: Iterable[str]) -> list[dict[str, Any]]:
    """One entry per expected check, in ``probe.EXPECTED`` order; a missing or garbled line fails its check."""
    observed: dict[str, str] = {}
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("check"), str) and isinstance(item.get("outcome"), str):
            observed.setdefault(item["check"], item["outcome"])
    checks = []
    for name, expected in probe.EXPECTED.items():
        outcome = observed.get(name, "missing")
        checks.append(
            {"check": name, "outcome": outcome, "expected": list(expected), "ok": probe.accepts(expected, outcome)}
        )
    return checks


class ThrowawaySessions:
    """The throw-away session and the "other" session (root only)."""

    def __init__(self) -> None:
        self.own = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.user = unit.user_name(self.own)
        self._created_user = False

    def create(self) -> None:
        used = {entry.pw_uid for entry in pwd.getpwall()} | {entry.gr_gid for entry in grp.getgrall()}
        uid = free_uid(used)
        base = unit.session_dir(self.own)
        useradd = ["useradd", "--uid", str(uid), "--user-group", "--no-create-home", "--home-dir", f"{base}/home"]
        # No subordinate UID/GID ranges (the session manager's rule, sessions.NO_SUBORDINATE_IDS).
        useradd += ["-K", "SUB_UID_COUNT=0", "-K", "SUB_GID_COUNT=0"]
        subprocess.run([*useradd, "--shell", "/usr/sbin/nologin", self.user], check=True, capture_output=True)
        self._created_user = True
        for path in (base, f"{base}/work", f"{base}/home", f"{base}/home/.claude"):
            os.makedirs(path, mode=0o700)
            os.chown(path, uid, uid)
        # Readable for everybody: only the sandbox may hide it, not the file permissions.
        other = unit.session_dir(self.other)
        os.makedirs(f"{other}/work")
        for path in (other, f"{other}/work"):
            os.chmod(path, 0o755)
        Path(f"{other}/work/secret.txt").write_text("other session\n", encoding="utf-8")
        os.chmod(f"{other}/work/secret.txt", 0o644)
        harness = Path(unit.harness_file(self.own))
        harness.parent.mkdir(mode=0o711, parents=True, exist_ok=True)
        harness.write_text("sandbox self-test\n", encoding="utf-8")
        harness.chmod(0o444)

    def remove(self) -> None:
        for path in (unit.session_dir(self.own), unit.session_dir(self.other)):
            shutil.rmtree(path, ignore_errors=True)
        Path(unit.harness_file(self.own)).unlink(missing_ok=True)
        if self._created_user:
            subprocess.run(["userdel", self.user], check=False, capture_output=True)


def run_selftest(config: SelftestConfig) -> dict[str, Any]:
    """Run the probe in a session unit and return the result JSON (``ok`` false on any deviation)."""
    sessions = ThrowawaySessions()
    unit_name = UNIT_PREFIX + uuid.UUID(sessions.own).hex
    try:
        sessions.create()
        spec = unit.SessionSpec(
            session_id=sessions.own,
            token=secrets.token_urlsafe(32),
            python=config.python,
            lib_dynload=config.lib_dynload,
            exec_paths=config.exec_paths,
            kind="subagent",
            claude=config.claude,
            launcher=config.launcher,
        )
        program = [config.python, "-I", config.probe, sessions.own, sessions.other, config.claude, "--"]
        program += unit.launcher_args(spec)
        command = unit.launcher_command(
            spec,
            program,
            extra_exec=[config.python, *config.extra_exec],
            extra_ro=config.extra_ro,
            extra_connect=config.extra_connect,
        )
        argv = unit.systemd_run(
            spec,
            command,
            unit_name=unit_name,
            mode="pipe",
            extra_properties=config.extra_properties,
        )
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=config.timeout, check=False, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired:
            result = subprocess.CompletedProcess(argv, -1, "", f"self-test timed out after {config.timeout} s")
    finally:
        # A killed `systemd-run --wait` leaves its unit running: stop it before its user and files go away.
        subprocess.run(["systemctl", "stop", unit_name], check=False, capture_output=True)
        sessions.remove()
    checks = evaluate(result.stdout.splitlines())
    return {
        "ok": result.returncode == 0 and all(check["ok"] for check in checks),
        "checks": checks,
        "kernel": os.uname().release,
        "landlock_abi": landlock.Kernel().abi(),
        "unit_exit": result.returncode,
        "stderr": result.stderr[-STDERR_TAIL:],
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Callable[[SelftestConfig], dict[str, Any]] = run_selftest,
    out: IO[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="agent_guest.sandbox.selftest", description="Sandbox self-test.")
    parser.add_argument("--exec-paths-file", default=unit.EXEC_PATHS_FILE)
    parser.add_argument("--build-file", default=unit.BUILD_FILE)
    parser.add_argument("--python", default="/usr/bin/python3")
    args = parser.parse_args(argv)
    python, lib_dynload = unit.interpreter_paths(args.python)
    exec_paths = unit.load_exec_paths(args.exec_paths_file, args.build_file)
    result = run(SelftestConfig(python=python, lib_dynload=lib_dynload, exec_paths=exec_paths))
    stream = out if out is not None else sys.stdout
    stream.write(json.dumps(result, sort_keys=True, indent=1) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
