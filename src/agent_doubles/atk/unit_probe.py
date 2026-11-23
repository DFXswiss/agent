"""Extended OS probe of the attack suite (ATS-03; 05-runner.md sections 4.3-4.5, ADR-008): runs **inside** a session
unit exactly like the guest's sandbox self-test probe (``agent_guest.sandbox.probe``), started by
``guest_probe.py os-extended`` through ``agent_guest.sandbox.selftest.run_selftest`` (same throw-away session, same
``systemd-run`` properties, same Landlock launcher), as the session user. Standard library only; it is executed as a
file (``python3 -I unit_probe.py OWN OTHER CLAUDE -- SESSION_LAUNCHER_OPTIONS``).

It assumes what the attack suite cannot otherwise show: code execution inside a session, with hooks and Claude Code
out of the way. It first restricts itself with the session's own Landlock ruleset (the guest probe's
``restrict_like_a_session``), runs every check of the guest probe (``CHECKS``, compared by the self-test with
``probe.EXPECTED``) and then the checks of ``EXTRA``, each printed as one JSON line ``{"check", "outcome"}``:

Checks (``EXPECTED`` holds the accepted outcomes):

- ``ld_so_python_contained``: the ELF interpreter maps ``python3`` (Landlock cannot refuse the mapping, decisions
  SBX-02 no. 25) and that Python tries every escape of ``CONTAINED`` -> ``contained`` (all refused) or a failed start;
- ``mmap_exec_workspace``, ``mmap_exec_tmp``: a file of the workspace / ``/tmp`` mapped executable -> ``EPERM`` or
  ``EACCES`` (``noexec``);
- ``proc_1_environ``: the environment of PID 1 -> hidden (``ProtectProc=invisible``);
- ``ptrace``: ``ptrace(PTRACE_TRACEME)`` in a child -> killed by the system call filter (``@debug``) or ``EPERM``;
- ``unix_sockets_root_list``: a connection to every listening unix socket of the VM as root lists them (``SOCKETS``,
  written by the root side; the guest probe's own ``unix_sockets`` reads the table from inside the unit) -> ``none``.

The guest probe itself checks fileless exec (``memfd_exec``) and the unix sockets it sees (ATS-03, ADR-045).

Outcomes as in ``agent_guest.sandbox.probe``: ``ok``, ``errno:<NAME>``, ``exit:<n>``, ``signal:<NAME>``,
``error:<exception>`` (never accepted) or a check-specific word.

Interface: ``EXTRA``, ``EXPECTED``, ``CONTAINED``, ``contained_outcome(stdout, code)``,
``main(argv=None, *, out=None, guest=None) -> int``.
"""

from __future__ import annotations

import ctypes
import errno
import functools
import importlib.util
import json
import mmap
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import IO, Any

GUEST_PROBE = "/opt/agent-guest/agent_guest/sandbox/probe.py"
PUBLIC_DIR = "/var/lib/atk-suite-probe"
SOCKETS = f"{PUBLIC_DIR}/sockets.json"
TIMEOUT = 10.0
PTRACE_TRACEME = 0
# What the Python that the ELF interpreter started tries (one outcome per attempt, printed as one JSON object).
CONTAINED = r"""
import json, os, socket, subprocess, sys
own, other = sys.argv[1], sys.argv[2]
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return "errno:" + str(exc.errno)
    except Exception as exc:
        return "error:" + type(exc).__name__
def write(path):
    with open(path, "w") as fh:
        fh.write("x")
def read(path):
    with open(path, "rb") as fh:
        fh.read(1)
def connect():
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
print(json.dumps({
    "exec_sh": attempt(lambda: subprocess.run(["/bin/sh", "-c", "exit 0"], check=True)),
    "exec_python": attempt(lambda: subprocess.run([sys.executable, "-c", "pass"], check=True)),
    "write_etc": attempt(lambda: write("/etc/atk-contained")),
    "write_var_tmp": attempt(lambda: write("/var/tmp/atk-contained")),
    "read_other_session": attempt(lambda: read("/srv/sessions/" + other + "/work/secret.txt")),
    "connect_external": attempt(connect),
}))
"""


@dataclass(frozen=True)
class Context:
    own: str
    other: str
    interpreter: str
    python: str
    work: str
    sockets: Sequence[str] = ()


def _errno_name(code: int) -> str:
    return errno.errorcode.get(code, str(code))


def contained_outcome(stdout: str, code: int) -> str:
    """``contained`` when the started Python reported only refused attempts, ``escaped:<names>`` otherwise; a Python
    that did not start (the mapping failed) is its exit code."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return f"exit:{code}"
    report = json.loads(lines[-1])
    escaped = sorted(name for name, outcome in report.items() if not str(outcome).startswith("errno:"))
    return f"escaped:{','.join(escaped)}" if escaped else "contained"


def _ld_so_python(ctx: Context) -> str:
    argv = [ctx.interpreter, ctx.python, "-I", "-c", CONTAINED, ctx.own, ctx.other]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT * 3, check=False)
    return contained_outcome(result.stdout, result.returncode)


def _mmap_exec(path: str) -> str:
    with open(path, "wb") as handle:
        handle.write(b"\0" * mmap.PAGESIZE)
    try:
        with open(path, "rb") as handle:
            mapped = mmap.mmap(handle.fileno(), mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_EXEC)
            mapped.close()
    finally:
        os.unlink(path)
    return "ok"


def _read(path: str) -> str:
    with open(path, "rb") as handle:
        handle.read(1)
    return "ok"


def _traceme() -> int:
    """The child's ``ptrace(PTRACE_TRACEME)``: 0, or 100 + the error number."""
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.ptrace(PTRACE_TRACEME, 0, None, None)
    return 0 if result == 0 else 100 + ctypes.get_errno() % 100


def _ptrace(
    fork: Callable[[], int] = os.fork,
    child: Callable[[], int] = _traceme,
    leave: Callable[[int], None] = os._exit,
    wait: Callable[[int, int], tuple[int, int]] = os.waitpid,
) -> str:
    """``PTRACE_TRACEME`` in a child (the system call filter kills it with SIGSYS, or the call fails)."""
    pid = fork()
    if pid == 0:  # the child reports through its exit status only
        leave(child())
        return "child"
    _pid, status = wait(pid, 0)
    if os.WIFSIGNALED(status):
        return f"signal:{signal.Signals(os.WTERMSIG(status)).name}"
    code = os.WEXITSTATUS(status)
    return "ok" if code == 0 else f"errno:{_errno_name(code - 100)}"


def _unix_sockets(ctx: Context, connect: Callable[[str], bool]) -> str:
    if not ctx.sockets:
        return "error:no_sockets_listed"
    reached = [address for address in ctx.sockets if connect(address)]
    return "none" if not reached else f"reachable:{','.join(reached)}"


EXTRA: dict[str, Callable[[Context], str]] = {
    "ld_so_python_contained": _ld_so_python,
    "mmap_exec_workspace": lambda ctx: _mmap_exec(f"{ctx.work}/atk-mmap"),
    "mmap_exec_tmp": lambda ctx: _mmap_exec("/tmp/atk-mmap"),
    "proc_1_environ": lambda ctx: _read("/proc/1/environ"),
    "ptrace": lambda ctx: _ptrace(),
}
EXPECTED: dict[str, tuple[str, ...]] = {
    "ld_so_python_contained": ("contained", "exit:nonzero"),
    "mmap_exec_workspace": ("errno:EPERM", "errno:EACCES"),
    "mmap_exec_tmp": ("errno:EPERM", "errno:EACCES"),
    "proc_1_environ": ("errno:ENOENT", "errno:EACCES", "errno:ESRCH"),
    "ptrace": ("signal:SIGSYS", "errno:EPERM"),
    "unix_sockets_root_list": ("none",),
}


def _load_guest(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_guest_probe", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} not found")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sockets(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            listed: list[str] = json.load(handle)
    except (OSError, ValueError):
        return []
    return listed


def main(argv: Sequence[str] | None = None, *, out: IO[str] | None = None, guest: ModuleType | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    stream = out if out is not None else sys.stdout
    if "--" not in args or args.index("--") != 3:
        stream.write("usage: unit_probe.py OWN_SESSION_ID OTHER_SESSION_ID CLAUDE_BINARY -- SESSION_LAUNCHER_OPTIONS\n")
        return 2
    own, other, claude = args[:3]
    probe = guest if guest is not None else _load_guest(GUEST_PROBE)
    listed = _sockets(SOCKETS)  # read before the session's own ruleset hides the suite's directory
    interpreter = ""

    def layer2() -> str:
        nonlocal interpreter
        interpreter = probe.restrict_like_a_session(claude, args[4:])
        return "ok"

    results: list[dict[str, Any]] = [{"check": "layer2", "outcome": probe.observe(layer2)}]
    context = probe.Context(own=own, other=other, claude=claude, interpreter=interpreter or "/nonexistent")
    results += probe.run_checks(context)
    extra = Context(
        own=own,
        other=other,
        interpreter=interpreter or "/nonexistent",
        python=os.path.realpath(sys.executable),
        work=context.work,
        sockets=listed,
    )
    results += [{"check": name, "outcome": probe.observe(functools.partial(fn, extra))} for name, fn in EXTRA.items()]
    reached = probe.observe(functools.partial(_unix_sockets, extra, probe.connect_unix))
    results.append({"check": "unix_sockets_root_list", "outcome": reached})
    for item in results:
        stream.write(json.dumps(item, sort_keys=True) + "\n")
    stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
