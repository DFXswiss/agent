"""Test hooks of the session-context scenarios (KOR-33, ADR-059; E2E-40 `session-context-resume`). Only for specs,
never a product path; VM commands through `orchestrator/guestvm.py`, store reads like `runner_store.py`.

- `POST /vms/{vm}/delete`: the VM disappears under its runner (deleted by hand, lost with its disk). The runner whose
  `AGENT_RUNNER_VM_PREFIX` the name carries is stopped, the instance deleted with `limactl delete -f` and the runner
  started again; its start-up reconciliation marks the row `deleted` (05-runner.md section 3.5), which the route waits
  for (`runner.vm`). Answers `{runner, vm}`.
- `POST /sessions/{session}/agent-kill`: SIGKILL to the main process of the session's unit (Claude Code after the
  launcher's `execv`) in its VM: the guest ends the session `agent_exited` (a failure, not a stop). Answers `{vm, pid}`.
- `POST /vms/{vm}/sessions/{session}/expire`: what the guest's retention does with a session that ended seven days ago
  (`agent_guest.sessions.SessionManager.cleanup`), without waiting a week: the session directory (transcript and
  workspace), the harness file, the metadata file and the session user are removed. Answers `{vm, existed}`
  (`existed`: the session directory was there before).
- `POST /runners/{name}/contexts/evict` (`{"task": "<id>"}`): the runner forgets its local encrypted checkpoints of the
  task (rows of `runner.context_object` and the directories `<home>/contexts/<object>`), like a device that never held
  them: a restore must read the hub's copy. Answers `{objects: [[object_id, state], ...]}` (the states before).
- `POST /hub-db/contexts/tamper` (`{"task": "<id>", "action": "corrupt" | "delete" | "uncommit", "objects": [...]}`):
  a damaged or rolled-back hub, as the database superuser. `corrupt` flips one ciphertext byte of chunk 0 of every
  complete checkpoint of the task (the stored SHA-256 stays, as a hub that lies would leave it); `delete` removes every
  checkpoint of the task (chunks by cascade); `uncommit` turns the complete checkpoints named in `objects` (required
  for it) back into uploads in progress - chunks stored, no commit -, i.e. an upload interrupted before its commit.
  Answers `{objects}` (the IDs changed).
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
from orchestrator.routes.runner_store import SCRIPT as STORE_QUERY
from orchestrator.services import HUB_DATABASE, Runner

DELETE_TIMEOUT_S = 300.0
SETTLE_TIMEOUT_S = 120.0
SETTLE_POLL_S = 1.0
QUERY_TIMEOUT_S = 60.0
META_DIR = "/var/lib/agent-guest/sessions"
HARNESS_DIR = "/run/agent-guest/harness"
TAMPER_ACTIONS = ("corrupt", "delete", "uncommit")
MAX_OBJECTS = 16
CLOCK: Callable[[], float] = time.monotonic
SLEEP: Callable[[float], None] = time.sleep
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VM = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Deletes the task's rows and their cache directories in one transaction of the runner's store; prints the rows.
EVICT_SCRIPT = """import json, shutil, sys
from pathlib import Path
import psycopg
home = Path(sys.argv[1]).expanduser()
port = (home / "pg" / "port").read_text(encoding="utf-8").strip()
with psycopg.connect(f"host=127.0.0.1 port={port} user=agent dbname=agent_runner") as conn:
    rows = conn.execute(
        "DELETE FROM runner.context_object WHERE task_id = %s RETURNING object_id::text, state", (sys.argv[2],)
    ).fetchall()
for object_id, _ in rows:
    shutil.rmtree(home / "contexts" / object_id, ignore_errors=True)
print(json.dumps(sorted([object_id, state] for object_id, state in rows)))
"""


def _uuid(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise RouteError(400, "invalid", f"{what} must be a lower-case UUID")
    return value


def _vm_name(ctx: Context, value: str) -> str:
    if not _VM.fullmatch(value) or not value.startswith(ctx.config.run_prefix):
        raise RouteError(400, "invalid", f"{value} is not a VM of this run")
    return value


def _owner(ctx: Context, vm: str) -> Runner:
    """The runner whose VM prefix `vm` carries (the longest one: `runner-1` and spawned `r<n>-` prefixes nest)."""
    found = [
        runner
        for runner in ctx.runners.values()
        if runner.env.get("AGENT_RUNNER_VM_PREFIX") and vm.startswith(runner.env["AGENT_RUNNER_VM_PREFIX"])
    ]
    if not found:
        raise RouteError(404, "not_found", f"no runner owns {vm}")
    return max(found, key=lambda runner: len(runner.env["AGENT_RUNNER_VM_PREFIX"]))


def _store(ctx: Context, runner: Runner, script: str, *args: str) -> Any:
    done = runner.node.run(
        [ctx.agent_python, "-c", script, runner.env["AGENT_RUNNER_HOME"], *args],
        env=runner.env,
        timeout=QUERY_TIMEOUT_S,
    )
    if done["rc"] != 0:
        raise RouteError(
            500, "failed", f"{runner.name}: store script exited with {done['rc']}: {done['stderr'][-300:]}"
        )
    return json.loads(done["stdout"])


def _vm_state(ctx: Context, runner: Runner, vm: str) -> str | None:
    rows = _store(ctx, runner, STORE_QUERY, f"SELECT state FROM runner.vm WHERE lima_name = '{vm}'")
    return str(rows[0]["state"]) if rows else None


def post_vm_delete(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    vm = _vm_name(ctx, params["vm"])
    runner = _owner(ctx, vm)
    if runner.state() != "stopped":
        runner.stop()
    done = ctx.runner_node.run(["limactl", "delete", "-f", vm], timeout=DELETE_TIMEOUT_S)
    runner.start()
    if done["rc"] != 0:
        raise RouteError(500, "failed", f"limactl delete {vm} exited with {done['rc']}: {done['stderr'][-300:]}")
    deadline = CLOCK() + SETTLE_TIMEOUT_S
    while (state := _vm_state(ctx, runner, vm)) not in (None, "deleted"):
        if CLOCK() > deadline:
            raise RouteError(500, "failed", f"{runner.name} still lists {vm} as {state}")
        SLEEP(SETTLE_POLL_S)
    return 200, {"runner": runner.name, "vm": vm}


def post_agent_kill(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    session = params["session"]
    try:
        vm = guestvm.session_vm(ctx, session)
        pid = guestvm.checked(ctx, vm, ["systemctl", "show", "-p", "MainPID", "--value", guestvm.UNIT_PREFIX + session])
        if not pid.strip().isdigit() or pid.strip() == "0":
            raise RouteError(409, "invalid", f"the unit of session {session} is not running")
        guestvm.checked(ctx, vm, ["kill", "-KILL", pid.strip()])
    except guestvm.GuestVmError as exc:
        raise RouteError(500, "failed", str(exc)) from None
    return 200, {"vm": vm, "pid": int(pid.strip())}


def post_expire(ctx: Context, params: dict[str, str], _body: Any) -> tuple[int, Any]:
    vm = _vm_name(ctx, params["vm"])
    session = params["session"]
    try:
        user = guestvm.user_name(session)
    except guestvm.GuestVmError as exc:
        raise RouteError(400, "invalid", str(exc)) from None
    directory = f"{guestvm.SESSIONS_ROOT}/{session}"
    try:
        existed = guestvm.shell(ctx, vm, ["test", "-d", directory])["rc"] == 0
        guestvm.checked(ctx, vm, ["rm", "-rf", directory])
        guestvm.checked(ctx, vm, ["rm", "-f", f"{HARNESS_DIR}/{session}.md", f"{META_DIR}/{session}.json"])
        guestvm.shell(ctx, vm, ["userdel", user])  # gone already (an earlier call) is fine
        guestvm.shell(ctx, vm, ["groupdel", user])
    except guestvm.GuestVmError as exc:
        raise RouteError(500, "failed", str(exc)) from None
    return 200, {"vm": vm, "existed": existed}


def post_evict(ctx: Context, params: dict[str, str], body: Any) -> tuple[int, Any]:
    runner = ctx.runners.get(params["name"])
    if runner is None:
        raise RouteError(404, "not_found", f"no runner {params['name']}")
    task = _uuid(body.get("task") if isinstance(body, dict) else None, "task")
    return 200, {"objects": _store(ctx, runner, EVICT_SCRIPT, task)}


def tamper_sql(task: str, action: str, objects: list[str]) -> str:
    if action == "corrupt":
        return (
            "UPDATE hub.context_chunk c SET ct = set_byte(c.ct, 20, get_byte(c.ct, 20) # 255)"
            " FROM hub.context_object o WHERE o.id = c.object_id AND c.chunk_index = 0"
            f" AND o.task_id = '{task}' AND o.state = 'complete' RETURNING c.object_id"
        )
    if action == "delete":
        return f"DELETE FROM hub.context_object WHERE task_id = '{task}' RETURNING id"
    listed = ", ".join(f"'{object_id}'" for object_id in objects)
    return (
        "UPDATE hub.context_object SET state = 'uploading', completed_at = NULL"
        f" WHERE task_id = '{task}' AND state = 'complete' AND id IN ({listed}) RETURNING id"
    )


def post_tamper(ctx: Context, _params: dict[str, str], body: Any) -> tuple[int, Any]:
    payload = body if isinstance(body, dict) else {}
    task = _uuid(payload.get("task"), "task")
    action = payload.get("action")
    if action not in TAMPER_ACTIONS:
        raise RouteError(400, "invalid", f"action must be one of {', '.join(TAMPER_ACTIONS)}")
    raw = payload.get("objects", [])
    if not isinstance(raw, list) or len(raw) > MAX_OBJECTS or (action == "uncommit" and not raw):
        raise RouteError(400, "invalid", f"objects must be a list of at most {MAX_OBJECTS} IDs (required for uncommit)")
    objects = [_uuid(item, "an object") for item in raw]
    if ctx.cluster is None:
        raise RouteError(409, "invalid", "the hub database is not running")
    out = ctx.cluster.psql(tamper_sql(task, str(action), objects), HUB_DATABASE)
    return 200, {"objects": sorted(line.strip() for line in out.splitlines() if line.strip())}


ROUTES = [
    Route("POST", "/vms/{vm}/delete", post_vm_delete),
    Route("POST", "/sessions/{session}/agent-kill", post_agent_kill),
    Route("POST", "/vms/{vm}/sessions/{session}/expire", post_expire),
    Route("POST", "/runners/{name}/contexts/evict", post_evict),
    Route("POST", "/hub-db/contexts/tamper", post_tamper),
]
