"""Kernel refusals of a session (KOR-60, decision D16; `agent_guest.metrics` `sandbox_denials`). Only for specs,
never a product path:

- `POST /sessions/{session}/locked-file` (`{"name": "<file name>", "text": "…"}`): root writes `name` into the
  session's workspace, owned by root with mode 0600 - a file inside the workspace that the PreToolUse policy lets the
  session read but the kernel refuses (the session user is neither owner nor in the group). It stands for anything a
  hook lets through that the second layer stops. Answers `{vm, path}`.
- `GET /sessions/{session}/guest-metrics`: the session's entry of the guest's `GET /v1/metrics` (read as root over the
  control socket with the control token, as the runner reads it), `null` when the guest does not list the session.
  Answers `{vm, session}`.
"""

from __future__ import annotations

import json
import re
from typing import Any

from orchestrator import guestvm
from orchestrator.context import Context
from orchestrator.control import Route, RouteError

MAX_TEXT = 4096
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Root: write the file with the mode set before any byte is in it, then hand it to root:root.
LOCK_SCRIPT = """import os, sys
path, text = sys.argv[1], sys.argv[2]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(text)
os.chown(path, 0, 0)
os.chmod(path, 0o600)
"""
# Root: `GET /v1/metrics` on the guest's control socket with the control token.
METRICS_SCRIPT = """import http.client, json, socket, sys
class Unix(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect("/run/agent-guest/control.sock")
with open("/etc/agent-guest/control-token", encoding="ascii") as handle:
    token = handle.read().strip()
connection = Unix("localhost")
connection.request("GET", "/v1/metrics", headers={"Authorization": f"Bearer {token}"})
response = connection.getresponse()
body = response.read().decode("utf-8")
if response.status != 200:
    sys.exit(f"GET /v1/metrics: {response.status} {body[:200]}")
print(body)
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


def post_locked_file(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    name = body.get("name") if isinstance(body, dict) else None
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not isinstance(text, str) or len(text) > MAX_TEXT:
        raise RouteError(400, "invalid", f'body must be {{"name": "<file name>", "text": "<at most {MAX_TEXT}>"}}')
    session = params["session"]
    vm = _vm(ctx, session)
    path = f"{guestvm.SESSIONS_ROOT}/{session}/work/{name}"
    _checked(ctx, vm, ["python3", "-I", "-c", LOCK_SCRIPT, path, text])
    return 200, {"vm": vm, "path": path}


def get_guest_metrics(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    vm = _vm(ctx, session)
    answer = json.loads(_checked(ctx, vm, ["python3", "-I", "-c", METRICS_SCRIPT]))
    found = [item for item in answer.get("sessions", []) if item.get("session_id") == session]
    return 200, {"vm": vm, "session": found[0] if found else None}


ROUTES = [
    Route("POST", "/sessions/{session}/locked-file", post_locked_file),
    Route("GET", "/sessions/{session}/guest-metrics", get_guest_metrics),
]
