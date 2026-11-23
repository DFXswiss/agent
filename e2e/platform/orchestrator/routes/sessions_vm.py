"""Test hooks on a session's VM (SCN-02; helpers in `orchestrator/guestvm.py`). Only for specs, never a product path:

- `GET /sessions/{session}/vm` → `{vm, user, uid, unit, main_pid, command}`: the VM whose tmux server has the session,
  the session user and its UID, the unit `agent-session-<id>` (`ActiveState`), its main PID and that process's
  command line (after the launcher's `execv` the Claude Code arguments, e.g. `--resume`) (E2E-10, E2E-11, E2E-13).
- `GET /sessions/{session}/clock` → `{vm, offset_ms, rtt_ms}`: the VM clock minus this device's clock (the hub device,
  whose clock the browser shares), from the sample with the shortest round trip, read at its end (`date` runs after
  the connection and the process start); its error is the way back, below `rtt_ms`
  (E2E-12 corrects the event times of the guest with it).
- `GET /sessions/{session}/cast` → `{vm, cast}`: the recorder's asciicast copy `/srv/sessions/<id>/terminal.cast`
  (E2E-12 compares the history with it).
- `POST /sessions/{session}/type` (`{"text": "…", "enter": true}`): types `text` straight into the session's tmux pane
  (`send-keys -l`), then `Enter`: the "typed into the pane" input that the prompt-origin hook must refuse (E2E-12,
  E2E-35). Answers `{vm}`.
- `POST /sessions/{session}/os-read` (`{"path": "…"}`): reads `path` with the session user's identity, once inside
  the mount namespace of the session's unit (what the sandbox sees) and once in the VM's root namespace (UID and
  directory modes alone). Answers `{vm, in_sandbox: {ok, error, data}, as_user: {ok, error, data}}` (`error` the
  `OSError` class, e.g. `FileNotFoundError`, `PermissionError`) (E2E-10).
- `POST /vms/files` (`{"paths": ["…"], "session": "<id>"?}`): `{vm: {path: {sha256, mode, owner} | null}}` for the
  session's VM or every running VM of the run (a booting VM is waited for, a deleted one left out) (E2E-34:
  configuration unchanged).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError

MAX_TYPED = 2000
CLOCK_SAMPLES = 5
CLOCK: Callable[[], float] = time.time
MAX_PATHS = 32
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,1023}$")
# Root: optionally enter the mount namespace of a process (`os.setns`, no exec inside the sandbox's noexec mounts),
# drop to the session user, then try to read. Prints {"ok", "error", "data"}.
READ_SCRIPT = """import json, os, pwd, sys
user, pid, path = sys.argv[1], sys.argv[2], sys.argv[3]
entry = pwd.getpwnam(user)
if pid:
    with open(f"/proc/{pid}/ns/mnt") as handle:
        os.setns(handle.fileno(), os.CLONE_NEWNS)
os.setgroups([])
os.setgid(entry.pw_gid)
os.setuid(entry.pw_uid)
try:
    with open(path, "rb") as handle:
        data = handle.read(65536).decode("utf-8", "replace")
    print(json.dumps({"ok": True, "error": None, "data": data}))
except OSError as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__, "data": ""}))
"""
FILES_SCRIPT = """import grp, hashlib, json, os, pwd, stat, sys
out = {}
for path in sys.argv[1:]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        out[path] = None
        continue
    digest = None
    if stat.S_ISREG(info.st_mode):
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    owner = f"{pwd.getpwuid(info.st_uid).pw_name}:{grp.getgrgid(info.st_gid).gr_name}"
    out[path] = {"sha256": digest, "mode": oct(stat.S_IMODE(info.st_mode)), "owner": owner}
print(json.dumps(out))
"""


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


def _path(value: Any) -> str:
    if not isinstance(value, str) or not _PATH_RE.fullmatch(value) or "/../" in f"{value}/":
        raise RouteError(400, "invalid", "a path must be absolute, without '..', of [A-Za-z0-9._/-]")
    return value


def get_vm(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    vm = _vm(ctx, session)
    user = guestvm.user_name(session)
    uid = int(_checked(ctx, vm, ["id", "-u", user]).strip())
    unit = guestvm.UNIT_PREFIX + session
    shown = _checked(ctx, vm, ["systemctl", "show", "-p", "ActiveState", "-p", "MainPID", unit])
    fields = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
    state, pid = fields.get("ActiveState", ""), int(fields.get("MainPID", "0") or 0)
    command: list[str] = []
    if pid:
        done = guestvm.shell(ctx, vm, ["cat", f"/proc/{pid}/cmdline"])
        command = [arg for arg in done["stdout"].split("\0") if arg] if done["rc"] == 0 else []
    return 200, {"vm": vm, "user": user, "uid": uid, "unit": state, "main_pid": pid, "command": command}


def get_clock(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    vm = _vm(ctx, params["session"])
    best: tuple[float, float] | None = None
    for _ in range(CLOCK_SAMPLES):
        before = CLOCK()
        text = _checked(ctx, vm, ["date", "+%s.%N"])
        after = CLOCK()
        # the VM's clock is read at the end of the round trip (after ssh, limactl and the process start), so the
        # offset is `remote - after`; the midpoint would be off by half that start-up time (SCN-08)
        rtt, offset = after - before, float(text.strip()) - after
        if best is None or rtt < best[0]:
            best = (rtt, offset)
    assert best is not None
    return 200, {"vm": vm, "offset_ms": round(best[1] * 1000, 1), "rtt_ms": round(best[0] * 1000, 1)}


def get_cast(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    vm = _vm(ctx, session)
    cast = _checked(ctx, vm, ["cat", f"{guestvm.SESSIONS_ROOT}/{session}/terminal.cast"])
    return 200, {"vm": vm, "cast": cast}


def post_type(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    text = body.get("text") if isinstance(body, dict) else None
    enter = body.get("enter", True) if isinstance(body, dict) else None
    if not isinstance(text, str) or not 0 < len(text) <= MAX_TYPED or not isinstance(enter, bool):
        raise RouteError(400, "invalid", f'body must be {{"text": "<1..{MAX_TYPED} characters>", "enter": bool}}')
    session = params["session"]
    vm = _vm(ctx, session)
    target = f"=s-{session}:"
    tmux = ["tmux", "-S", guestvm.TMUX_SOCKET, "send-keys", "-t", target]
    _checked(ctx, vm, [*tmux, "-l", "--", text])
    if enter:
        _checked(ctx, vm, [*tmux, "Enter"])
    return 200, {"vm": vm}


def post_os_read(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    path = _path(body.get("path") if isinstance(body, dict) else None)
    session = params["session"]
    vm = _vm(ctx, session)
    user = guestvm.user_name(session)
    shown = _checked(ctx, vm, ["systemctl", "show", "-p", "MainPID", "--value", guestvm.UNIT_PREFIX + session])
    pid = shown.strip()
    if not pid.isdigit() or pid == "0":
        raise RouteError(409, "invalid", f"the unit of session {session} is not running")
    out: dict[str, Any] = {"vm": vm}
    for key, namespace in (("in_sandbox", pid), ("as_user", "")):
        done = guestvm.shell(ctx, vm, ["python3", "-I", "-c", READ_SCRIPT, user, namespace, path])
        out[key] = json.loads(done["stdout"]) if done["rc"] == 0 else {"error": done["stderr"].strip()[-300:]}
    return 200, out


def post_files(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    paths = body.get("paths") if isinstance(body, dict) else None
    session = body.get("session") if isinstance(body, dict) else None
    if not isinstance(paths, list) or not 0 < len(paths) <= MAX_PATHS:
        raise RouteError(400, "invalid", f'body must be {{"paths": [1..{MAX_PATHS} absolute paths], "session"?}}')
    checked = [_path(path) for path in paths]
    if session is not None and not isinstance(session, str):
        raise RouteError(400, "invalid", "session must be a session ID")
    vms = [_vm(ctx, session)] if session is not None else guestvm.running_vms(ctx)
    out = {}
    for vm in vms:
        done = guestvm.shell_when_up(ctx, vm, ["python3", "-I", "-c", FILES_SCRIPT, *checked])
        if done is None:
            continue  # deleted by its runner meanwhile
        if done["rc"] != 0:
            raise RouteError(500, "failed", f"file facts in {vm} exited with {done['rc']}: {done['stderr'][-300:]}")
        out[vm] = json.loads(done["stdout"])
    return 200, out


ROUTES = [
    Route("GET", "/sessions/{session}/vm", get_vm),
    Route("GET", "/sessions/{session}/cast", get_cast),
    Route("GET", "/sessions/{session}/clock", get_clock),
    Route("POST", "/sessions/{session}/type", post_type),
    Route("POST", "/sessions/{session}/os-read", post_os_read),
    Route("POST", "/vms/files", post_files),
]
