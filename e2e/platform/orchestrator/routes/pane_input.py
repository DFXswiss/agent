"""Pane input log of a session (SCN-06, E2E-26: "the tmux pane receives no input bytes", R-12.10). Only for specs,
never a product path; helpers in `orchestrator/guestvm.py`.

Input reaches a tmux pane through the tmux server, which writes it into the pane's pty master: keys of `send-keys`,
pasted buffers (`paste-buffer`, the guest's prompt delivery), keys of attached clients and the server's answers to
terminal queries of the program. The log watches the server's writes to exactly that descriptor. Other ways into the
terminal (another process with the master open, `TIOCSTI` on the slave) are outside it; the guest delivers input only
through tmux. The trace file holds every write of the server (pane output included)
until `stop` removes it; a spec that fails before `stop` leaves it in the run's VM, which the run deletes.

- `POST /sessions/{session}/pane-input/start` → `{vm, server_pid, tty, fd}`: finds the tmux server of the session's
  VM (`#{pid}`), the pane's terminal `/dev/pts/<n>` (`#{pane_tty}`) and the server's `/dev/ptmx` descriptor with
  `tty-index: <n>` (`/proc/<pid>/fdinfo`), then attaches `strace` to the server in the transient unit
  `e2e-pane-input-<session>` (write calls only, strings in hex, to `/run/e2e-pane-input-<session>.trace`) and waits
  until the kernel reports the tracer (`TracerPid`). An earlier log of the session is replaced.
- `POST /sessions/{session}/pane-input/stop` → `{vm, fd, writes, bytes, data}`: refuses (409) when the unit is no
  longer running (strace ended early: the log would be incomplete), stops it, counts the successful writes to the
  pane's descriptor and their bytes (`data`: the first `MAX_DATA` bytes as text) and removes the files.

The TEST_MODE golden keeps root for these hooks (ADR-036); `strace` is part of the Ubuntu base image.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError

UNIT_PREFIX = "e2e-pane-input-"
ATTACH_TIMEOUT_S = 10.0
POLL_S = 0.2
MAX_DATA = 4096
SLEEP: Callable[[float], None] = time.sleep
CLOCK: Callable[[], float] = time.monotonic
# Root in the VM: the descriptor of `/dev/ptmx` in process argv[1] whose pty has index argv[2].
MASTER_SCRIPT = """import os, sys
pid, index = sys.argv[1], sys.argv[2]
for fd in sorted(os.listdir(f"/proc/{pid}/fd"), key=int):
    try:
        if os.readlink(f"/proc/{pid}/fd/{fd}") != "/dev/ptmx":
            continue
        with open(f"/proc/{pid}/fdinfo/{fd}") as handle:
            if any(line.split() == ["tty-index:", index] for line in handle):
                print(fd)
                break
    except OSError:
        continue
"""
_PANE_RE = re.compile(r"^(\d+) /dev/pts/(\d+)$")
_CALL_RE = re.compile(r"^(?:write|writev|pwrite64)\((\d+), (.*) = (-?\d+)(?: .*)?$")
_STRING_RE = re.compile(r'"((?:\\x[0-9a-f]{2})*)"')
_TRACER_RE = re.compile(r"^TracerPid:\s*(\d+)", re.MULTILINE)


def _paths(session: str) -> tuple[str, str, str]:
    unit = UNIT_PREFIX + session
    return unit, f"/run/{unit}.trace", f"/run/{unit}.fd"


def _vm(ctx: Context, session: str) -> str:
    try:
        return guestvm.session_vm(ctx, session)
    except guestvm.GuestVmError as exc:
        raise RouteError(404, "not_found", str(exc)) from None


def _checked(ctx: Context, vm: str, argv: list[str]) -> str:
    try:
        return guestvm.checked(ctx, vm, argv)
    except guestvm.GuestVmError as exc:
        raise RouteError(500, "failed", str(exc)) from None


def parse_trace(text: str, fd: int) -> tuple[int, bytes]:
    """Successful write calls to `fd` in strace output (`-xx`) and the bytes they wrote."""
    writes, data = 0, bytearray()
    for line in text.splitlines():
        found = _CALL_RE.match(line)
        if found is None or int(found.group(1)) != fd or int(found.group(3)) < 0:
            continue
        writes += 1
        written = b"".join(bytes.fromhex(s.replace("\\x", "")) for s in _STRING_RE.findall(found.group(2)))
        data += written[: int(found.group(3))]
    return writes, bytes(data)


def summary(vm: str, fd: int, text: str) -> dict[str, Any]:
    writes, data = parse_trace(text, fd)
    return {
        "vm": vm,
        "fd": fd,
        "writes": writes,
        "bytes": len(data),
        "data": data[:MAX_DATA].decode("utf-8", "replace"),
    }


def post_start(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    vm = _vm(ctx, session)
    unit, trace, fd_file = _paths(session)
    shown = _checked(
        ctx,
        vm,
        ["tmux", "-S", guestvm.TMUX_SOCKET, "display-message", "-p", "-t", f"=s-{session}:", "#{pid} #{pane_tty}"],
    ).strip()
    pane = _PANE_RE.fullmatch(shown)
    if pane is None:
        raise RouteError(500, "failed", f"unexpected pane of session {session}: {shown!r}")
    pid, index = pane.groups()
    fd = _checked(ctx, vm, ["python3", "-I", "-c", MASTER_SCRIPT, pid, index]).strip()
    if not fd.isdigit():
        raise RouteError(500, "failed", f"no pty master of /dev/pts/{index} in the tmux server {pid}")
    guestvm.shell(ctx, vm, ["systemctl", "stop", unit])
    _checked(ctx, vm, ["rm", "-f", trace, fd_file])
    _checked(ctx, vm, ["sh", "-c", f"printf %s {fd} > {fd_file}"])
    strace = ["strace", "-qq", "-p", pid, "-e", "trace=write,writev,pwrite64", "-e", "signal=none", "-xx"]
    _checked(ctx, vm, ["systemd-run", f"--unit={unit}", "--collect", "-q", *strace, "-s", "65536", "-o", trace])
    deadline = CLOCK() + ATTACH_TIMEOUT_S
    while True:
        status = _checked(ctx, vm, ["grep", "-s", "TracerPid", f"/proc/{pid}/status"])
        tracer = _TRACER_RE.search(status)
        if tracer is not None and tracer.group(1) != "0":
            break
        if CLOCK() >= deadline:
            raise RouteError(500, "failed", f"strace did not attach to the tmux server {pid} of {vm}")
        SLEEP(POLL_S)
    return 200, {"vm": vm, "server_pid": int(pid), "tty": f"/dev/pts/{index}", "fd": int(fd)}


def post_stop(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    vm = _vm(ctx, session)
    unit, trace, fd_file = _paths(session)
    active = guestvm.shell(ctx, vm, ["systemctl", "is-active", unit])["stdout"].strip()
    if active != "active":
        raise RouteError(409, "failed", f"the pane input log of session {session} is not running ({active})")
    _checked(ctx, vm, ["systemctl", "stop", unit])
    fd = _checked(ctx, vm, ["cat", fd_file]).strip()
    if not fd.isdigit():
        raise RouteError(500, "failed", f"no descriptor recorded for the pane input log of {session}")
    text = _checked(ctx, vm, ["sh", "-c", f"grep -E '^(write|writev|pwrite64)\\({fd}, ' {trace} || true"])
    _checked(ctx, vm, ["rm", "-f", trace, fd_file])
    return 200, summary(vm, int(fd), text)


ROUTES = [
    Route("POST", "/sessions/{session}/pane-input/start", post_start),
    Route("POST", "/sessions/{session}/pane-input/stop", post_stop),
]
