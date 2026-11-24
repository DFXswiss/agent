"""Sandbox probe of the self-test (docs/platform/spec/05-runner.md section 4.5); runs inside a session unit.

``agent_guest.sandbox.selftest`` starts it in a unit with the properties of a real session and the session's
Landlock rules, where the outer launcher layer additionally lets the interpreter execute::

    python3 -I probe.py <own session id> <other session id> <claude binary> -- <session launcher options>

The probe first installs the session's Landlock ruleset once more (``landlock.plan`` of the session options) as a
second layer, so its own interpreter is no longer executable, exactly like a session after ``execv``; then it tries
what a hook bypass would allow and
prints one JSON line ``{"check": NAME, "outcome": OUTCOME}`` per attempt. ``EXPECTED`` holds the accepted outcomes;
``selftest.evaluate`` compares them. Outcomes: ``ok`` (the operation worked), ``errno:<NAME>``, ``exit:<code>``
(a started program failed), ``gaierror`` (name resolution failed), ``timeout``, ``error:<exception>`` (an
unexpected failure; never accepted) or a check-specific word.

Standard library only; this file is executed without the package on ``sys.path`` and loads ``landlock.py`` from
its own directory.

Interface: ``CHECKS`` (name -> function of ``Context``), ``EXPECTED``, ``accepts(expected, outcome) -> bool``,
``observe(fn) -> str``, ``run_checks(context) -> list[dict]``, ``restrict_like_a_session(claude, args) -> str``,
``main(argv=None, *, out=None) -> int``.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import functools
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import IO

SESSIONS_ROOT = "/srv/sessions"
PLATFORM_PORT = 7700
CLONE_NEWUSER = 0x10000000
EXTERNAL = ("1.1.1.1", 443)
OTHER_LOCAL_PORT = 8787
TIMEOUT = 5.0
NONZERO_EXIT = "exit:nonzero"
HIDDEN = ("mode:0", "errno:ENOENT")  # replaced by an inaccessible node, or absent
SO_ACCEPTCON = 0x10000
UNIX_UNCONNECTED = 7
SO_PASSCRED = 16
SYS_IO_URING_SETUP = 425  # generic system call table (aarch64 and x86_64)


@dataclass(frozen=True)
class Context:
    own: str
    other: str
    claude: str
    interpreter: str
    sessions_root: str = SESSIONS_ROOT
    external: tuple[str, int] = EXTERNAL
    platform_port: int = PLATFORM_PORT
    other_local_port: int = OTHER_LOCAL_PORT
    proc: str = "/proc"

    @property
    def work(self) -> str:
        return f"{self.sessions_root}/{self.own}/work"


def _run(argv: Sequence[str]) -> str:
    result = subprocess.run(list(argv), capture_output=True, timeout=TIMEOUT * 2, check=False)
    return "ok" if result.returncode == 0 else f"exit:{result.returncode}"


def _write(path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("probe\n")
    os.unlink(path)
    return "ok"


def _read(path: str) -> str:
    with open(path, "rb") as fh:
        fh.read(1)
    return "ok"


def _copy_binary(source: str, target: str) -> str:
    shutil.copyfile(source, target)
    os.chmod(target, 0o755)
    return target


def _connect(host: str, port: int) -> str:
    with socket.create_connection((host, port), timeout=TIMEOUT):
        return "ok"


def _bind() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
    return "ok"


def _unix_bind_abstract() -> str:
    """Bind an abstract unix name: another session of the VM could reach it (ADR-064 section 4)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(f"\0agent-selftest-{os.getpid()}")
    return "ok"


def _unix_listen() -> str:
    """``listen`` on a socket with an automatic abstract name (Linux refuses ``listen`` on an unbound unix socket with
    ``EINVAL``): the name comes from ``bind`` with an empty address, which the unit refuses as well - ``listen`` is
    then still tried, so the outcome is the one of ``listen`` alone."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        with contextlib.suppress(OSError):
            sock.bind("")
        sock.listen(1)
    return "ok"


def _unix_passcred() -> str:
    """``SO_PASSCRED`` makes ``connect``/``sendmsg`` bind the socket to an abstract name automatically."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, SO_PASSCRED, 1)
    return "ok"


def _io_uring() -> str:
    """``io_uring_setup``: newer kernels bind and listen through io_uring."""
    libc = ctypes.CDLL(None, use_errno=True)
    params = ctypes.create_string_buffer(120)  # struct io_uring_params
    descriptor = libc.syscall(SYS_IO_URING_SETUP, 1, params)
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    os.close(descriptor)
    return "ok"


def _unshare() -> str:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(CLONE_NEWUSER) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return "ok"


def _stat(path: str) -> str:
    return f"mode:{os.stat(path).st_mode & 0o7777:o}"


def _sendto(host: str, port: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"probe", (host, port))
    return "ok"


def _memfd_exec() -> str:
    """A copy of ``/usr/bin/true`` in a ``memfd``, executed through ``/proc/self/fd`` (fileless: Landlock and
    ``NoExecPaths`` do not see the kernel's internal mount; ``vm.memfd_noexec=2`` of the image refuses it, ATS-03)."""
    create: Callable[[str, int], int] | None = getattr(os, "memfd_create", None)  # Linux only
    if create is None:
        raise OSError(errno.ENOSYS, "memfd_create is not available")
    descriptor = create("agent-selftest", 0)
    try:
        with open("/usr/bin/true", "rb") as source:
            os.write(descriptor, source.read())
        path = f"/proc/self/fd/{descriptor}"
        result = subprocess.run([path], pass_fds=(descriptor,), capture_output=True, timeout=TIMEOUT, check=False)
    finally:
        os.close(descriptor)
    return "ok" if result.returncode == 0 else f"exit:{result.returncode}"


def listening_unix(table: str) -> list[str]:
    """Addresses of the listening unix sockets in the text of ``/proc/net/unix`` (``@name`` for abstract ones):
    stream and seqpacket sockets in listening state, and bound, unconnected datagram sockets."""
    found = set()
    for line in table.splitlines()[1:]:
        fields = line.split(None, 7)
        if len(fields) < 8:
            continue
        flags, kind, state = int(fields[3], 16), int(fields[4], 16), int(fields[5], 16)
        if flags & SO_ACCEPTCON or (kind == socket.SOCK_DGRAM and state == UNIX_UNCONNECTED):
            found.add(fields[7])
    return sorted(found)


def connect_unix(address: str) -> bool:
    """Whether a stream, datagram or seqpacket connection to the socket (``@name`` = abstract) is accepted."""
    target = "\0" + address[1:] if address.startswith("@") else address
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
        try:
            with socket.socket(socket.AF_UNIX, kind) as sock:
                sock.settimeout(TIMEOUT)
                sock.connect(target)
        except OSError:
            continue
        return True
    return False


def _unix_sockets(ctx: Context) -> str:
    """Every listening unix socket of the VM's network namespace that the session could reach (system services
    activated as root, the resolver's varlink API - a way out past the egress rules -, abstract sockets, which no
    mount namespace hides). The unit sees an empty ``/run`` (ATS-03)."""
    with open(f"{ctx.proc}/self/net/unix", encoding="utf-8", errors="replace") as handle:
        listening = listening_unix(handle.read())
    reached = [address for address in listening if connect_unix(address)]
    return "none" if not reached else f"reachable:{','.join(reached)}"


def _sessions_visible(ctx: Context) -> str:
    entries = sorted(os.listdir(ctx.sessions_root))
    return "own_only" if entries == [ctx.own] else f"visible:{len(entries)}"


def _processes(ctx: Context) -> str:
    uid = os.getuid()
    foreign = 0
    for pid in filter(str.isdigit, os.listdir(ctx.proc)):
        try:
            owner = os.stat(f"{ctx.proc}/{pid}").st_uid
        except FileNotFoundError:
            continue  # the process ended meanwhile
        foreign += owner != uid
    return "own_only" if not foreign else f"foreign:{foreign}"


CHECKS: dict[str, Callable[[Context], str]] = {
    "exec_sh": lambda ctx: _run(["/bin/sh", "-c", "exit 0"]),
    "exec_git": lambda ctx: _run(["/usr/bin/git", "--version"]),
    "exec_python3": lambda ctx: _run(["/usr/bin/python3", "-c", "pass"]),
    "exec_workspace_copy": lambda ctx: _run([_copy_binary("/usr/bin/id", f"{ctx.work}/probe-id")]),
    "ld_so_system_binary": lambda ctx: _run([ctx.interpreter, "/usr/bin/id"]),
    "ld_so_workspace_binary": lambda ctx: _run([ctx.interpreter, _copy_binary("/usr/bin/id", f"{ctx.work}/probe-ld")]),
    "write_workspace": lambda ctx: _write(f"{ctx.work}/probe-file"),
    "write_tmp": lambda ctx: _write("/tmp/agent-selftest-probe"),
    "write_etc": lambda ctx: _write("/etc/agent-selftest-probe"),
    "write_opt_claude": lambda ctx: _write(f"{os.path.dirname(ctx.claude)}/agent-selftest-probe"),
    "write_var_tmp": lambda ctx: _write("/var/tmp/agent-selftest-probe"),
    "write_dev_shm": lambda ctx: _write("/dev/shm/agent-selftest-probe"),
    "list_sessions": _sessions_visible,
    "stat_other_session": lambda ctx: _stat(f"{ctx.sessions_root}/{ctx.other}"),
    "read_other_session": lambda ctx: _read(f"{ctx.sessions_root}/{ctx.other}/work/secret.txt"),
    "guest_secrets_masked": lambda ctx: _stat("/etc/agent-guest"),
    "system_bus_hidden": lambda ctx: _stat("/run/dbus/system_bus_socket"),
    "systemd_private_hidden": lambda ctx: _stat("/run/systemd/private"),
    "transient_units_hidden": lambda ctx: _stat("/run/systemd/transient"),
    "list_processes": _processes,
    "tcp_external": lambda ctx: _connect(*ctx.external),
    "udp_external": lambda ctx: _sendto(ctx.external[0], 53),
    "tcp_other_local_port": lambda ctx: _connect("127.0.0.1", ctx.other_local_port),
    "tcp_bind": lambda ctx: _bind(),
    "dns": lambda ctx: str(len(socket.getaddrinfo("example.com", 443))),
    "tcp_platform": lambda ctx: _connect("127.0.0.1", ctx.platform_port),
    "unshare_user_namespace": lambda ctx: _unshare(),
    "memfd_exec": lambda ctx: _memfd_exec(),
    "unix_sockets": _unix_sockets,
    "unix_bind_abstract": lambda ctx: _unix_bind_abstract(),
    "unix_listen": lambda ctx: _unix_listen(),
    "unix_passcred": lambda ctx: _unix_passcred(),
    "io_uring": lambda ctx: _io_uring(),
}

# Accepted outcomes per check (05 section 4.5 plus positive controls and further checks, decisions SBX-02 15/16).
# Where possible the outcome names the layer: EACCES on connect/`/dev/shm` is Landlock, ENOENT for the other
# session is the unit's mount namespace, mode 0 is InaccessiblePaths, EPERM for UDP is the egress filter, EPERM for
# bind/listen/io_uring/SO_PASSCRED the system-call filters of unit and launcher (ADR-064).
EXPECTED: dict[str, tuple[str, ...]] = {
    "layer2": ("ok",),
    "exec_sh": ("errno:EACCES",),
    "exec_git": ("errno:EACCES",),
    "exec_python3": ("errno:EACCES",),
    "exec_workspace_copy": ("errno:EACCES",),
    "ld_so_system_binary": (NONZERO_EXIT, "errno:EACCES"),
    "ld_so_workspace_binary": (NONZERO_EXIT, "errno:EACCES"),
    "write_workspace": ("ok",),
    "write_tmp": ("ok",),
    "write_etc": ("errno:EROFS", "errno:EACCES"),
    "write_opt_claude": ("errno:EROFS", "errno:EACCES"),
    "write_var_tmp": ("errno:EROFS", "errno:EACCES"),
    "write_dev_shm": ("errno:EACCES",),
    "list_sessions": ("own_only", "errno:EACCES"),
    "stat_other_session": ("errno:ENOENT",),
    "read_other_session": ("errno:ENOENT",),
    "guest_secrets_masked": ("mode:0",),
    "system_bus_hidden": HIDDEN,
    "systemd_private_hidden": HIDDEN,
    "transient_units_hidden": ("mode:0", "errno:ENOENT"),
    "list_processes": ("own_only",),
    "tcp_external": ("errno:EACCES",),
    "udp_external": ("errno:EPERM",),
    "tcp_other_local_port": ("errno:EACCES",),
    "tcp_bind": ("errno:EPERM",),
    "dns": ("gaierror",),
    "tcp_platform": ("ok",),
    "unshare_user_namespace": ("errno:EPERM",),
    "memfd_exec": ("errno:EACCES",),
    "unix_sockets": ("none",),
    # ADR-064 section 4: the unit refuses bind, listen and io_uring, the launcher SO_PASSCRED (all EPERM)
    "unix_bind_abstract": ("errno:EPERM",),
    "unix_listen": ("errno:EPERM",),
    "unix_passcred": ("errno:EPERM",),
    "io_uring": ("errno:EPERM",),
}


def accepts(expected: Sequence[str], outcome: str) -> bool:
    """Whether ``outcome`` is one of ``expected``; ``exit:nonzero`` stands for every ``exit:<n>`` with n != 0."""
    if outcome in expected:
        return True
    return NONZERO_EXIT in expected and outcome.startswith("exit:") and outcome != "exit:0"


def observe(fn: Callable[[], str]) -> str:
    """Run one attempt and classify its result."""
    try:
        return fn()
    except socket.gaierror:
        return "gaierror"
    except (TimeoutError, subprocess.TimeoutExpired):
        return "timeout"
    except OSError as exc:
        return f"errno:{errno.errorcode.get(exc.errno or 0, exc.errno)}"
    except Exception as exc:  # any other failure is an outcome no expectation accepts
        return f"error:{type(exc).__name__}"


def run_checks(ctx: Context) -> list[dict[str, str]]:
    return [{"check": name, "outcome": observe(functools.partial(fn, ctx))} for name, fn in CHECKS.items()]


def _landlock() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_guest_landlock", Path(__file__).with_name("landlock.py"))
    if spec is None or spec.loader is None:
        raise ImportError("landlock.py not found next to probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the class body runs
    spec.loader.exec_module(module)
    return module


def restrict_like_a_session(claude: str, session_args: Sequence[str]) -> str:
    """Second layer: the session's own ruleset (``landlock.plan``), so the interpreter that started the probe is no
    longer executable, exactly like a session after ``execv``. Returns the ELF interpreter of the Claude binary."""
    landlock = _landlock()
    interpreter = landlock.elf_interpreter(claude)
    if interpreter is None:
        raise OSError(errno.ENOEXEC, f"{claude} has no ELF interpreter")
    kernel = landlock.Kernel()
    options = landlock.parse_args([*session_args, "--", claude])
    landlock.apply(landlock.plan(options, kernel.abi()), kernel)
    return str(interpreter)


def main(argv: Sequence[str] | None = None, *, out: IO[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    stream = out if out is not None else sys.stdout
    if "--" not in args or args.index("--") != 3:
        stream.write("usage: probe.py OWN_SESSION_ID OTHER_SESSION_ID CLAUDE_BINARY -- SESSION_LAUNCHER_OPTIONS\n")
        return 2
    own, other, claude = args[:3]
    session_args = args[4:]
    interpreter = ""

    def layer2() -> str:
        nonlocal interpreter
        interpreter = restrict_like_a_session(claude, session_args)
        return "ok"

    results = [{"check": "layer2", "outcome": observe(layer2)}]
    results += run_checks(Context(own=own, other=other, claude=claude, interpreter=interpreter or "/nonexistent"))
    for item in results:
        stream.write(json.dumps(item, sort_keys=True) + "\n")
    stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
