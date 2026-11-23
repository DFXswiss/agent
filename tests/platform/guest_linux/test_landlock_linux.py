"""Landlock launcher on a real kernel (05-runner.md section 4.3): exec, write and connect are refused as specified.

The launcher runs as a script (exactly like in the session unit) or, to observe the restricted process from the
inside, through a small driver that installs the same plan and then tries the operations. The driver may write
coverage data into the checkout (an extra --rw path), everything else is the session ruleset.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_guest.sandbox import landlock
from agent_guest.sandbox.selftest import ThrowawaySessions

from .conftest import LAUNCHER, SRC, Guest

pytestmark = pytest.mark.linux

DRIVER = textwrap.dedent(
    """
    import json, os, shutil, socket, stat, subprocess, sys
    from agent_guest.sandbox import landlock, probe

    argv, operations = json.loads(sys.argv[1]), json.loads(sys.argv[2])
    options = landlock.parse_args(argv)
    landlock.apply(landlock.plan(options, landlock.Kernel().abi()), landlock.Kernel())
    if operations.get("second_layer"):
        second = landlock.parse_args(operations["second_layer"])
        landlock.apply(landlock.plan(second, landlock.Kernel().abi()), landlock.Kernel())

    def run(args):
        return probe._run(args)

    def write(path):
        return probe._write(path)

    def read(path):
        return probe._read(path)

    def write_device(path):
        with open(path, "w") as fh:
            fh.write("x")
        return "ok"

    def mknod(path):
        os.mknod(path, 0o600 | stat.S_IFCHR, os.makedev(1, 3))
        os.unlink(path)
        return "ok"

    def mkdir(path):
        os.mkdir(path)
        os.rmdir(path)
        return "ok"

    def copy_exec(path):
        return run([probe._copy_binary("/usr/bin/true", path)])

    def connect(port):
        return probe._connect("127.0.0.1", port)

    def bind(_):
        return probe._bind()

    actions = dict(run=run, write=write, read=read, write_device=write_device, mknod=mknod, mkdir=mkdir,
                   copy_exec=copy_exec, connect=connect, bind=bind)
    out = {}
    for name, (action, arg) in operations["ops"].items():
        out[name] = probe.observe(lambda: actions[action](arg))
    print(json.dumps(out))
    """
)


def launcher_args(sessions: ThrowawaySessions, guest: Guest, *extra: str) -> list[str]:
    session_dir = f"/srv/sessions/{sessions.own}"
    return ["--session-dir", session_dir, "--exec", guest.claude, "--connect", "7700", *extra]


def drive(
    guest: Guest, args: list[str], ops: dict[str, tuple[str, object]], second_layer: list[str] | None = None
) -> dict[str, str]:
    """Run the driver with coverage data allowed into the checkout; returns the observed outcomes."""
    argv = [*args, "--rw", str(Path.cwd()), "--", guest.claude]
    payload = json.dumps({"ops": ops, "second_layer": second_layer or []})
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", DRIVER, json.dumps(argv), payload], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    outcome: dict[str, str] = json.loads(result.stdout)
    return outcome


def test_kernel_offers_landlock_abi_4_or_newer(capsys: pytest.CaptureFixture[str]) -> None:
    abi = landlock.Kernel().abi()
    with capsys.disabled():
        print(f"\nguest-ci: kernel {os.uname().release}, Landlock ABI {abi}")
    assert abi >= landlock.MIN_ABI


def test_launcher_execs_the_allowed_program(sessions: ThrowawaySessions, guest: Guest) -> None:
    argv = [guest.python, "-I", str(LAUNCHER), *launcher_args(sessions, guest), "--", guest.claude, "hello"]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert (result.returncode, result.stdout) == (0, "hello\n"), result.stderr


def test_launcher_refuses_a_program_without_exec_rule(sessions: ThrowawaySessions, guest: Guest) -> None:
    argv = [guest.python, "-I", str(LAUNCHER), *launcher_args(sessions, guest), "--", "/usr/bin/true"]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 126
    assert "is not an --exec file" in result.stderr


def test_launcher_with_harness_file(sessions: ThrowawaySessions, guest: Guest) -> None:
    harness = f"/run/agent-guest/harness/{sessions.own}.md"
    args = launcher_args(sessions, guest, "--harness", harness)
    result = subprocess.run(
        [guest.python, "-I", str(LAUNCHER), *args, "--", guest.claude, "ok"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "ok\n", result.stderr
    assert drive(guest, args, {"read_harness": ("read", harness)}) == {"read_harness": "ok"}


def test_ruleset_refuses_exec_write_and_connect(sessions: ThrowawaySessions, guest: Guest) -> None:
    work = f"/srv/sessions/{sessions.own}/work"
    outcome = drive(
        guest,
        launcher_args(sessions, guest),
        {
            "exec_claude": ("run", [guest.claude, "x"]),
            "exec_sh": ("run", ["/bin/sh", "-c", "true"]),
            "exec_python": ("run", ["/usr/bin/python3", "-c", "pass"]),
            "exec_git": ("run", ["/usr/bin/git", "--version"]),
            "exec_workspace_copy": ("copy_exec", f"{work}/true"),
            "exec_tmp_copy": ("copy_exec", "/tmp/landlock-test-true"),
            "write_workspace": ("write", f"{work}/file"),
            "mkdir_workspace": ("mkdir", f"{work}/dir"),
            "write_tmp": ("write", "/tmp/landlock-test-file"),
            "write_dev_null": ("write_device", "/dev/null"),
            "write_etc": ("write", "/etc/landlock-test"),
            "write_var_tmp": ("write", "/var/tmp/landlock-test"),
            "write_other_session": ("write", f"/srv/sessions/{sessions.other}/work/x"),
            "mknod_workspace": ("mknod", f"{work}/null"),
            "read_etc": ("read", "/etc/hostname"),
            "read_var": ("read", "/var/lib/dpkg/status"),
            "read_root_home": ("read", "/root/.profile"),
            "read_harness_without_rule": ("read", f"/run/agent-guest/harness/{sessions.own}.md"),
            "connect_platform": ("connect", 7700),
            "connect_other_port": ("connect", 8787),
            "bind": ("bind", None),
        },
    )
    eacces = f"errno:{errno.errorcode[errno.EACCES]}"
    assert outcome == {
        "exec_claude": "ok",
        "exec_sh": eacces,
        "exec_python": eacces,
        "exec_git": eacces,
        "exec_workspace_copy": eacces,
        "exec_tmp_copy": eacces,
        "write_workspace": "ok",
        "mkdir_workspace": "ok",
        "write_tmp": "ok",
        "write_dev_null": "ok",
        "write_etc": eacces,
        "write_var_tmp": eacces,
        "write_other_session": eacces,
        "mknod_workspace": eacces,
        "read_etc": "ok",
        "read_var": eacces,
        "read_root_home": eacces,
        "read_harness_without_rule": eacces,
        "connect_platform": "ok",
        "connect_other_port": eacces,
        "bind": eacces,
    }


def test_ld_so_is_not_stopped_by_landlock_alone(sessions: ThrowawaySessions, guest: Guest) -> None:
    """Landlock checks execve, not mmap(PROT_EXEC): the unit's noexec mounts close this (test_unit_linux.py)."""
    outcome = drive(guest, launcher_args(sessions, guest), {"ld_so": ("run", [guest.interpreter, "/usr/bin/true"])})
    assert outcome == {"ld_so": "ok"}


def test_second_layer_drops_the_interpreter(sessions: ThrowawaySessions, guest: Guest) -> None:
    args = launcher_args(sessions, guest, "--exec", guest.python)
    ops: dict[str, tuple[str, object]] = {
        "python": ("run", [guest.python, "-c", "pass"]),
        "claude": ("run", [guest.claude, "x"]),
    }
    assert drive(guest, args, ops) == {"python": "ok", "claude": "ok"}
    session = [*launcher_args(sessions, guest), "--rw", str(Path.cwd()), "--", guest.claude]
    assert drive(guest, args, ops, second_layer=session) == {"python": "errno:EACCES", "claude": "ok"}


def test_kernel_reports_errors_of_the_real_system_calls(tmp_path: Path) -> None:
    kernel = landlock.Kernel()
    with pytest.raises(OSError, match="landlock_create_ruleset") as empty:
        kernel.create_ruleset(0, 0)
    assert empty.value.errno == errno.ENOMSG
    ruleset = kernel.create_ruleset(landlock.handled_fs(4), 0)
    try:
        with pytest.raises(OSError, match="landlock_add_rule") as bad_access:
            kernel.add_path_rule(ruleset, str(tmp_path), 1 << 40)
        assert bad_access.value.errno == errno.EINVAL
        with pytest.raises(OSError, match="landlock_add_rule port") as no_net:
            kernel.add_port_rule(ruleset, 7700, landlock.NET_CONNECT_TCP)  # network not handled by this ruleset
        assert no_net.value.errno == errno.EINVAL
    finally:
        kernel.close(ruleset)
    with pytest.raises(OSError, match="landlock_restrict_self") as bad_fd:
        kernel.restrict_self(-1)
    assert bad_fd.value.errno == errno.EBADF
