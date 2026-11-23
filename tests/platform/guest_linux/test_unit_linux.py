"""Session unit properties take effect under systemd (05-runner.md sections 4.3 and 4.4).

A test binary (the system Python, which the unit's ExecPaths allow because the launcher needs it) reports from
inside the unit what it sees; the launcher test runs the real session command around the Claude stand-in.
"""

from __future__ import annotations

import json
import pwd
import shlex
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from agent_guest.sandbox import unit
from agent_guest.sandbox.selftest import ThrowawaySessions

from .conftest import Guest

pytestmark = pytest.mark.linux

INSPECT = textwrap.dedent(
    """
    import json, os, socket, subprocess, sys
    sys.path.insert(0, sys.argv[1])
    import probe
    out = {"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd(), "umask": oct(os.umask(0o077)),
           "env": sorted(os.environ), "home": os.environ.get("HOME")}
    status = dict(line.split(":", 1) for line in open("/proc/self/status").read().splitlines() if ":" in line)
    for key in ("NoNewPrivs", "CapBnd", "CapEff", "Seccomp"):
        out[key] = status[key].strip()
    mounts = {line.split()[4]: line.split()[5] for line in open("/proc/self/mountinfo")}
    out["tmp_opts"] = mounts.get("/tmp")
    out["sessions"] = sorted(os.listdir("/srv/sessions"))
    out["run"] = sorted(os.listdir("/run"))
    cgroup = open("/proc/self/cgroup").read().strip().split("::", 1)[1]
    for name in ("memory.max", "pids.max", "cpu.max"):
        out[name] = open(f"/sys/fs/cgroup{cgroup}/{name}").read().strip()
    tries = {
        "write_etc": lambda: probe._write("/etc/unit-test"),
        "write_usr": lambda: probe._write("/usr/unit-test"),
        "write_home": lambda: probe._write(os.environ["HOME"] + "/file"),
        "list_guest_secrets": lambda: str(os.listdir("/etc/agent-guest")),
        "exec_sh": lambda: probe._run(["/bin/sh", "-c", "true"]),
        "exec_tmp_copy": lambda: probe._run([probe._copy_binary("/usr/bin/true", "/tmp/t")]),
        "ld_so_system_binary": lambda: probe._run([sys.argv[2], "/usr/bin/true"]),
        "tcp_external": lambda: probe._connect("1.1.1.1", 443),
        "udp_dns": lambda: str(socket.getaddrinfo("example.com", 443)),
        "unshare": probe._unshare,
        "system_bus": lambda: probe._stat("/run/dbus/system_bus_socket"),
        "systemd_private": lambda: probe._stat("/run/systemd/private"),
        "transient_units": lambda: probe._stat("/run/systemd/transient"),
        "resolver": lambda: probe._stat("/run/systemd/resolve/io.systemd.Resolve"),
        "memfd_exec": probe._memfd_exec,
        "guest_secrets": lambda: probe._stat("/etc/agent-guest"),
        "processes": lambda: probe._processes(probe.Context("", "", "", "")),
    }
    for name, fn in tries.items():
        out[name] = probe.observe(fn)
    print(json.dumps(out))
    """
)


SYSTEMD_ENV = frozenset(
    {"INVOCATION_ID", "SYSTEMD_EXEC_PID", "USER", "LOGNAME", "SHELL", "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE"}
)


def run_unit(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)


def test_unit_properties_take_effect(spec: unit.SessionSpec, sessions: ThrowawaySessions, guest: Guest) -> None:
    command = [guest.python, "-I", "-c", INSPECT, str(Path(spec.launcher).parent), guest.interpreter]
    result = run_unit(unit.systemd_run(spec, command, unit_name=f"agent-unit-test-{uuid.uuid4().hex}", mode="pipe"))
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    entry = pwd.getpwnam(sessions.user)
    base = f"/srv/sessions/{sessions.own}"
    assert (seen["uid"], seen["gid"]) == (entry.pw_uid, entry.pw_gid)
    assert seen["cwd"] == f"{base}/work"
    assert seen["umask"] == "0o77"
    assert seen["home"] == f"{base}/home"
    # Only the allow-list; systemd adds its own bookkeeping variables, nothing is inherited from the caller.
    assert set(seen["env"]) - SYSTEMD_ENV == {key for key, _ in unit.environment(spec)}
    assert seen["NoNewPrivs"] == "1"
    assert int(seen["CapBnd"], 16) == 0
    assert int(seen["CapEff"], 16) == 0
    assert seen["Seccomp"] == "2"
    assert "noexec" in seen["tmp_opts"]
    assert seen["sessions"] == [sessions.own]
    assert seen["memory.max"] == str(4 * 1024**3)
    assert seen["pids.max"] == "256"
    assert seen["cpu.max"] == "200000 100000"
    assert seen["write_etc"] in ("errno:EROFS", "errno:EACCES")
    assert seen["write_usr"] in ("errno:EROFS", "errno:EACCES")
    assert seen["write_home"] == "ok"
    assert seen["list_guest_secrets"] == "errno:EACCES"
    assert seen["exec_sh"] == "errno:EACCES"  # NoExecPaths=/ (no Landlock here)
    assert seen["exec_tmp_copy"] == "errno:EACCES"
    assert seen["ld_so_system_binary"].startswith("exit:")  # mapping fails on the noexec mount
    # Dropped by the egress rules (silently: timeout) or by IPAddressDeny=any (EPERM); never connected.
    assert seen["tcp_external"] in ("timeout", "errno:EPERM")
    assert seen["udp_dns"] == "gaierror"
    assert seen["unshare"] == "errno:EPERM"
    assert seen["processes"] == "own_only"
    # unit environments (tokens) are not reachable over D-Bus: the unit sees an empty /run (ADR-045)
    assert seen["system_bus"] == "errno:ENOENT"
    assert seen["systemd_private"] == "errno:ENOENT"
    assert seen["transient_units"] == "errno:ENOENT"
    assert seen["resolver"] == "errno:ENOENT"
    assert set(seen["run"]) <= {"agent-guest", "systemd"}  # mount points systemd itself needs inside the tmpfs
    assert seen["memfd_exec"] == "errno:EACCES"  # vm.memfd_noexec = 2 of the image
    assert seen["guest_secrets"] == "mode:0"


def test_residual_risk_the_loader_can_start_python_inside_the_unit(spec: unit.SessionSpec, guest: Guest) -> None:
    """05 section 4.3 needs the interpreter in ExecPaths for the launcher, and Landlock cannot refuse the mapping:
    `ld.so python3` still runs, under the same Landlock, network and unit restrictions as Claude itself
    (decisions SBX-02 no. 25). This test pins the behaviour so that a fix (static launcher) is noticed."""
    sandbox_dir = Path(spec.launcher).parent
    session = [*unit.launcher_args(spec), "--", spec.claude]
    script = textwrap.dedent(
        f"""
        import importlib.util, subprocess, sys
        loader = importlib.util.spec_from_file_location("ll", {str(sandbox_dir / "landlock.py")!r})
        ll = importlib.util.module_from_spec(loader)
        sys.modules["ll"] = ll
        loader.loader.exec_module(ll)
        kernel = ll.Kernel()
        ll.apply(ll.plan(ll.parse_args({session!r}), kernel.abi()), kernel)
        try:
            subprocess.run([{guest.python!r}, "-c", "pass"])
        except PermissionError:
            print("exec refused")
        subprocess.run([{guest.interpreter!r}, {guest.python!r}, "-I", "-c", "print('mapped')"])
        """
    )
    direct = [guest.python, "-I", "-c", script]
    result = run_unit(unit.systemd_run(spec, direct, unit_name=f"agent-unit-test-{uuid.uuid4().hex}", mode="pipe"))
    assert result.returncode == 0, result.stderr
    assert sorted(result.stdout.splitlines()) == ["exec refused", "mapped"]


def test_session_command_runs_claude_inside_the_launcher(spec: unit.SessionSpec) -> None:
    command = unit.launcher_command(spec, [spec.claude, "hello", "world"])
    result = run_unit(unit.systemd_run(spec, command, unit_name=f"agent-unit-test-{uuid.uuid4().hex}", mode="pipe"))
    assert (result.returncode, result.stdout) == (0, "hello world\n"), result.stderr


def test_build_runs_with_a_pty_like_in_the_tmux_window(spec: unit.SessionSpec) -> None:
    argv = unit.build(spec, ["from", "the", "pty"])
    result = subprocess.run(
        ["script", "-qec", shlex.join(argv), "/dev/null"], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "from the pty" in result.stdout


def test_launcher_failure_ends_the_unit_with_126(spec: unit.SessionSpec) -> None:
    command = unit.launcher_command(spec, ["/usr/bin/true"])
    result = run_unit(unit.systemd_run(spec, command, unit_name=f"agent-unit-test-{uuid.uuid4().hex}", mode="pipe"))
    assert result.returncode == 126
    assert "is not an --exec file" in result.stderr
