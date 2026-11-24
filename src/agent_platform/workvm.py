"""The work VM of a session (11-agent-access.md section 8.4, 13-work-vm.md sections 8 and 10, ADR-059; twin of
`web/src/core/chat/workvm.ts`, `ui/screens/session/WorkvmPanel.tsx` and `WorkvmRuntime.tsx`).

- `parse_run` / `parse_state`: a decrypted `workvm.run` / `workvm.state` body checked against
  `contracts/events/objects.json` (every field required, no other field); anything else is None and counted as
  `invalid` (the web shows a neutral row instead);
- `latest_runs`: of the `workvm.run` events of one job only the latest is kept (the final one replaces the running
  one), as in the chat timeline;
- `workvm_runtime`: per work VM the `runtime_ms` of its `deleted` event, else `now - started_at` while it runs;
- `session workvm --session`: the session panel of the web: the phase of the session's work VM (`none`, `starting`
  while a `workvm.request` operation of the session is open and no VM of the session runs, with the countdown to the
  10 s target, `running`, `deleted` with its reason); the VM's facts, the runtime of the session's and of the task's
  work VMs (the header), and the runs of the session (latest per job, newest start first). The events are the task's
  `workvm.*` events of the workgroup scope, opened and verified like the chat; the operations are the first page of the
  task's list (`ensureOperations`). There is no release command: only the session can request or release its VM
  (decision UI-1 of KOR-29).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlencode

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.eventio import open_all, pull, rows_of
from agent_platform.hub import segment
from agent_platform.orgs import find_org
from agent_platform.sessions import OPERATIONS_LIMIT, session_row
from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import WG

RUN_KIND = "workvm.run"
STATE_KIND = "workvm.state"
REQUEST_TYPE = "workvm.request"
RUN_STATES = ("running", "exited", "killed", "timed_out", "lost")
REASONS = ("released", "idle", "session_ended", "task_closed", "failed", "restart")
FINAL_STATES = frozenset({"succeeded", "failed", "expired", "duplicate", "denied", "rejected"})
START_TARGET_MS = 10_000
MAX_SAFE_INTEGER = 2**53 - 1

Check = Callable[[object], bool]


def _pattern(expression: str) -> Check:
    compiled = re.compile(expression)
    return lambda value: isinstance(value, str) and compiled.fullmatch(value) is not None


def _one_of(*values: object) -> Check:
    """`values.includes(value)` of JavaScript: `true` is not `1`."""
    return lambda value: any(type(value) is type(item) and value == item for item in values)


def _integer(low: int, high: int) -> Check:
    """`Number.isSafeInteger` within the bounds (a JSON `5.0` is the number 5, a boolean is no number)."""
    return lambda value: (
        (type(value) is int or (type(value) is float and value.is_integer())) and low <= cast(float, value) <= high
    )


def _text(limit: int) -> Check:
    """JSON Schema `maxLength` counts code points."""
    return lambda value: isinstance(value, str) and len(value) <= limit


def _nullable(check: Check) -> Check:
    return lambda value: value is None or check(value)


def _time(value: object) -> bool:
    try:
        parse_time(value)
    except CryptoError:
        return False
    return True


_COUNT = _integer(0, MAX_SAFE_INTEGER)
_WORKVM_ID = _pattern(r"wvm-[0-9a-f]{8}")

RUN_SHAPE: Mapping[str, Check] = {
    "workvm_id": _WORKVM_ID,
    "job_id": _pattern(r"j-[0-9a-f]{12}"),
    "kind": _one_of("exec", "browse"),
    "command": _text(4096),
    "state": _one_of(*RUN_STATES),
    "exit_code": _nullable(_integer(0, 255)),
    "started_at": _time,
    "ended_at": _nullable(_time),
    "duration_ms": _COUNT,
    "output": _text(65536),
    "output_bytes": _COUNT,
}

STATE_SHAPE: Mapping[str, Check] = {
    "workvm_id": _WORKVM_ID,
    "state": _one_of("created", "deleted"),
    "scope": _one_of("task", "session"),
    "size": _one_of("standard", "large", "xlarge"),
    "cpus": _integer(1, 64),
    "memory_mib": _integer(512, 262144),
    "disk_gib": _integer(1, 4096),
    "implicit": _one_of(True, False),
    "reason": _nullable(_one_of(*REASONS)),
    "started_at": _time,
    "ended_at": _nullable(_time),
    "runtime_ms": _COUNT,
}


def _matches(body: object, shape: Mapping[str, Check]) -> bool:
    return isinstance(body, dict) and set(body) == set(shape) and all(shape[key](body[key]) for key in shape)


def parse_run(body: object) -> dict[str, Any] | None:
    """A `workvm.run` body, or None when it does not follow the contract."""
    return cast(dict[str, Any], body) if _matches(body, RUN_SHAPE) else None


def parse_state(body: object) -> dict[str, Any] | None:
    """A `workvm.state` body, or None when it does not follow the contract."""
    return cast(dict[str, Any], body) if _matches(body, STATE_SHAPE) else None


def output_truncated(run: Mapping[str, Any]) -> bool:
    """Whether the output is only the tail of the job's output (`output_bytes` counts the whole output in UTF-8)."""
    return len(str(run["output"]).encode("utf-8", "surrogatepass")) < int(run["output_bytes"])


def latest_runs(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`events` (in timeline order) without the `workvm.run` events a later event of the same job replaces; events
    with an invalid body are kept."""
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for event in reversed(events):
        run = parse_run(event.get("body")) if event["kind"] == RUN_KIND else None
        if run is not None:
            if run["job_id"] in seen:
                continue
            seen.add(run["job_id"])
        kept.append(event)
    kept.reverse()
    return kept


def _ms(text: object) -> int:
    return int(parse_time(text).timestamp() * 1000)


def _by_vm(states: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Per work VM its `deleted` state if there is one, else its first state."""
    found: dict[str, Mapping[str, Any]] = {}
    for state in states:
        if state["workvm_id"] not in found or state["state"] == "deleted":
            found[state["workvm_id"]] = state
    return found


def workvm_runtime(states: Iterable[Mapping[str, Any]], now: int) -> dict[str, Any]:
    """The summed runtime (ms) of the work VMs of `states`, their number and whether one still runs."""
    total = 0
    running = False
    vms = _by_vm(states)
    for state in vms.values():
        if state["state"] == "deleted":
            total += int(state["runtime_ms"])
        else:
            running = True
            total += max(0, now - _ms(state["started_at"]))
    return {"ms": total, "vms": len(vms), "running": running}


def workvm_panel(
    states: Sequence[Mapping[str, Any]], requests: Sequence[Mapping[str, Any]], now: int
) -> dict[str, Any]:
    """The state of the session's work VM (`workvmPanel`): `states` are the session's state bodies, `requests` its
    `workvm.request` operations."""
    vms = _by_vm(states)
    last: Mapping[str, Any] | None = None
    for state in vms.values():
        if last is None or _ms(state["started_at"]) >= _ms(last["started_at"]):
            last = state
    running = next((state for state in vms.values() if state["state"] == "created"), None)
    idle = {"elapsed_ms": 0, "remaining_ms": 0, "overdue": False}
    if running is not None:
        return {"phase": "running", "vm": running, **idle}
    opened = [_ms(row["created_at"]) for row in requests if row["state"] not in FINAL_STATES]
    if opened:
        elapsed = max(0, now - min(opened))
        return {
            "phase": "starting",
            "vm": last,
            "elapsed_ms": elapsed,
            "remaining_ms": max(0, START_TARGET_MS - elapsed),
            "overdue": elapsed > START_TARGET_MS,
        }
    return {"phase": "none" if last is None else "deleted", "vm": last, **idle}


def now_ms() -> int:
    return int(time.time() * 1000)


def _requests(account: Account, task: str, session: str) -> list[dict[str, Any]]:
    """The `workvm.request` operations of `session` on the first page of the task's operations."""
    query = urlencode({"limit": OPERATIONS_LIMIT})
    answer = account.call("GET", f"/v1/tasks/{segment(task)}/operations?{query}")
    found = []
    for row in rows_of(answer, "operations"):
        if (row.get("task_id"), row.get("type"), row.get("session_id")) != (task, REQUEST_TYPE, session):
            continue
        if not isinstance(row.get("state"), str) or not _time(row.get("created_at")):
            continue
        found.append({key: row.get(key) for key in ("operation_id", "state", "error_code", "created_at")})
    return found


def session_workvm(account: Account, session: str) -> dict[str, Any]:
    row = session_row(account, session)["session"]
    wg, task = str(row["wg_id"]), row.get("task_id")
    if not isinstance(task, str):
        raise unverified("response_invalid")
    items, _last = pull(account, wg, {"kinds": f"{RUN_KIND},{STATE_KIND}", "task": task})
    events, skipped = open_all(account, find_org(account, wg), (WG, wg), items)
    invalid = 0
    states: list[tuple[object, dict[str, Any]]] = []
    runs: dict[str, dict[str, Any]] = {}
    own: set[str] = set()
    for event in sorted(events, key=lambda found: int(found["seq"])):
        if event.get("locked"):
            continue
        if event["task"] != task or event["kind"] not in (RUN_KIND, STATE_KIND):
            skipped += 1  # slipped in by the hub
            continue
        if event["kind"] == RUN_KIND:
            run = parse_run(event.get("body"))
            invalid += run is None
            if run is not None and event["session"] == session:
                own.add(run["workvm_id"])
                runs.pop(run["job_id"], None)
                runs[run["job_id"]] = run
            continue
        state = parse_state(event.get("body"))
        invalid += state is None
        if state is not None:
            states.append((event["session"], state))
            if event["session"] == session:
                own.add(state["workvm_id"])
    of_task = [state for _session, state in states]
    of_session = [state for state in of_task if state["workvm_id"] in own]
    now = now_ms()
    panel = workvm_panel(of_session, _requests(account, task, session), now)
    task_runtime = workvm_runtime(of_task, now)
    ordered = sorted(enumerate(runs.values()), key=lambda pair: (-_ms(pair[1]["started_at"]), pair[0]))
    return {
        "session_id": session,
        "task_id": task,
        **panel,
        "runtime": {
            "session_ms": workvm_runtime(of_session, now)["ms"],
            "task_ms": task_runtime["ms"],
            "vms": task_runtime["vms"],
            "running": task_runtime["running"],
        },
        "runs": [{**run, "output_truncated": output_truncated(run)} for _index, run in ordered],
        "locked": sum(1 for event in events if event.get("locked")),
        "invalid": invalid,
        "unverified": skipped,
    }


def _workvm(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return session_workvm(account, args.session)


def register(tree: Tree) -> None:
    group = tree.group("session", "sessions of tasks")
    sub = tree.command(group, "workvm", _workvm, "the session's work VM: state, size, runtime and runs")
    sub.add_argument("--session", required=True)
