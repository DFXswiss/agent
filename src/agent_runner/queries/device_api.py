"""Task metadata from the hub's device endpoints (03-api.md section 6.1): `GET /v1/device/workgroups/{wg}/tasks` and
`GET /v1/device/tasks/{task}` (right `device:wg`).

The answers are verified against the pinned hub key by the hub client. The hub sees task metadata in plaintext (state,
owner, times; 04-crypto.md section 7) but titles only as `task.meta` events, so the reads take the titles from the
verified events (`sessions.profiles`). A hub answer is still only metadata of a party that must not widen the read
scope: every row of a task list must belong to the session's org and the listed workgroup, a single task must belong
to the session's org and to a workgroup the session may read (`ReadScope.readable`, ADR-089; any other is `not_found`
"There is no such task you can read.", whatever the reason, except the `forbidden` of a process analysis that reads
other workgroups only as figures, `rights.FiguresOnly`), and rows or fields that do not have the form of the
contract are dropped (lists) or make the read `unavailable` (a single task). A refusal `403`/`404` is "no such task"
(`None`); an unreachable hub or any other refusal is `unavailable`. The hub names the workgroup of a task, but the
reads take content only from events sealed in that workgroup's scope, so a hub that names a wrong workgroup yields no
content of another one.

KOR-52 (ADR-095 section 1): a task row may carry its `number` and a session row its `agent_path`, the handles of the
main agent and its subagents. A hub of an older release sends neither (`number` None, no path); a value that breaks the
contract drops the row like any other field (a path must also have one ordinal per level of the session's depth).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from agent_runner.queries.rights import FiguresOnly, ReadRefused, ReadScope
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import runtime

TASK_STATES = frozenset({"scheduling", "starting", "working", "idle", "failed", "closed"})
SESSION_STATES = frozenset(
    {
        "requested",
        "vm_assigning",
        "vm_ready",
        "sandbox_ready",
        "agent_starting",
        "prompt_delivering",
        "working",
        "idle",
        "suspended",
        "resuming",
        "stopping",
        "stopped",
        "failed",
    }
)
OPERATION_STATES = frozenset(
    {"received", "rejected", "accepted", "duplicate", "denied", "executing", "succeeded", "failed", "expired"}
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIME_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
OPERATION_TYPE_RE = re.compile(r"^(?=.{1,64}$)[a-z][a-z_]*(\.[a-z][a-z_]*)+$")
BEFORE_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
AGENT_PATH_RE = re.compile(r"^[1-9][0-9]{0,8}(\.[1-9][0-9]{0,8})*$")
NUMBER_MAX = 9007199254740991
MAX_SESSIONS, MAX_OPERATIONS = 100, 1000
"""The maxima of `api/device-task-get.response.json` (a hub answering more is cut to them)."""
HUB_UNAVAILABLE = ReadRefused("unavailable", "The platform is not reachable right now. Try again later.")
HUB_INVALID = ReadRefused("unavailable", "The platform answered with invalid task data.")
TASK_NOT_FOUND = ReadRefused("not_found", "There is no such task you can read.")


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    wg_id: str
    owner_id: str
    state: str
    created_at: str
    last_activity_at: str
    number: int | None = None
    """The task's number in its organization (`#<number>`), None from a hub that does not send it."""


def _uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def _time(value: object) -> bool:
    return isinstance(value, str) and TIME_RE.fullmatch(value) is not None


def _number(value: Mapping[str, Any]) -> tuple[bool, int | None]:
    """`(valid, number)` of a task row: absent or null is valid and None."""
    number = value.get("number")
    if number is None:
        return True, None
    return type(number) is int and 1 <= number <= NUMBER_MAX, number


def task_row(scope: ReadScope, value: object, wg_id: str) -> TaskRow | None:
    """A task row of the session's org and workgroup `wg_id` with the fields the reads return, else None."""
    if not isinstance(value, Mapping):
        return None
    number_valid, number = _number(value)
    valid = (
        number_valid
        and _uuid(value.get("task_id"))
        and value.get("org_id") == scope.org_id
        and value.get("wg_id") == wg_id
        and _uuid(value.get("owner_id"))
        and value.get("state") in TASK_STATES
        and _time(value.get("created_at"))
        and _time(value.get("last_activity_at"))
    )
    if not valid:
        return None
    return TaskRow(
        task_id=value["task_id"],
        wg_id=wg_id,
        owner_id=value["owner_id"],
        state=value["state"],
        created_at=value["created_at"],
        last_activity_at=value["last_activity_at"],
        number=number,
    )


async def _get(context: Any, target: str) -> dict[str, object] | None:
    sync = runtime(context)
    if sync is None:
        raise HUB_UNAVAILABLE
    try:
        return await sync.client.request("GET", target)
    except HubRefusal as exc:
        if exc.status in (403, 404):
            return None
        raise HUB_UNAVAILABLE from None
    except HubTransportError:
        raise HUB_UNAVAILABLE from None


async def tasks(
    context: Any, scope: ReadScope, wg_id: str, *, state: str | None, before: str | None, limit: int | None
) -> tuple[list[TaskRow], str | None]:
    """One page of the tasks of workgroup `wg_id` (the caller checked that the session may read it), newest first,
    and the hub's `next_before`."""
    pairs = (("state", state), ("before", before), ("limit", limit))
    query = {name: str(value) for name, value in pairs if value is not None}
    target = f"/v1/device/workgroups/{quote(wg_id)}/tasks"
    answer = await _get(context, target + (f"?{urlencode(query)}" if query else ""))
    if answer is None:
        return [], None
    listed = answer.get("tasks")
    found = (task_row(scope, item, wg_id) for item in (listed if isinstance(listed, list) else []))
    rows = [row for row in found if row]
    following = answer.get("next_before")
    if limit is not None and len(rows) > limit:
        # a hub answering more rows than asked: the rest is not reachable through its cursor, so none is given
        return rows[:limit], None
    return rows, following if isinstance(following, str) and BEFORE_RE.fullmatch(following) else None


@dataclass(frozen=True)
class TaskDetail:
    task: TaskRow
    sessions: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    agent_paths: dict[str, str] = field(default_factory=dict)
    """Session ID -> `agent_path` of the subagent sessions whose row carries one."""


def _path_fits(path: object, depth: int) -> bool:
    """An absent path fits every session (older hub); a present one is a subagent's, one ordinal per level."""
    if path is None:
        return True
    return isinstance(path, str) and AGENT_PATH_RE.fullmatch(path) is not None and path.count(".") + 1 == depth


def _session(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    depth, parent = value.get("depth"), value.get("parent_session_id")
    valid = (
        _uuid(value.get("session_id"))
        and value.get("state") in SESSION_STATES
        and type(depth) is int
        and depth >= 0
        and (parent is None or _uuid(parent))
        and _path_fits(value.get("agent_path"), depth)
    )
    if not valid:
        return None
    return {"id": value["session_id"], "state": value["state"], "depth": depth, "parent_session_id": parent}


def _operation(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    op_type = value.get("type")
    valid = (
        _uuid(value.get("id"))
        and isinstance(op_type, str)
        and OPERATION_TYPE_RE.fullmatch(op_type) is not None
        and value.get("state") in OPERATION_STATES
    )
    return {"id": value["id"], "type": op_type, "state": value["state"]} if valid else None


async def task(context: Any, scope: ReadScope, task_id: str) -> TaskDetail | None:
    """The task with its sessions and operations; None when the hub knows no such task (module docstring)."""
    answer = await _get(context, f"/v1/device/tasks/{quote(task_id)}")
    if answer is None:
        return None
    raw = answer.get("task")
    wg_id = raw.get("wg_id") if isinstance(raw, Mapping) else None
    if not isinstance(wg_id, str):
        raise HUB_INVALID
    if wg_id != scope.wg_id:
        try:
            scope.readable(wg_id)
        except FiguresOnly:
            raise
        except ReadRefused:
            # a task of a workgroup the session may not read (or this device cannot): the same answer as no task
            raise TASK_NOT_FOUND.at(wg_id) if wg_id in scope.state.workgroups else TASK_NOT_FOUND from None
    found = task_row(scope, raw, wg_id)
    if found is None or found.task_id != task_id:
        raise HUB_INVALID
    sessions, operations = answer.get("sessions"), answer.get("operations")
    rows = sessions if isinstance(sessions, list) else []
    kept = [(row, found_session) for row in rows if (found_session := _session(row)) is not None][:MAX_SESSIONS]
    return TaskDetail(
        task=found,
        sessions=[found_session for _row, found_session in kept],
        agent_paths={
            found_session["id"]: row["agent_path"]
            for row, found_session in kept
            if isinstance(row.get("agent_path"), str)
        },
        operations=[o for o in map(_operation, operations if isinstance(operations, list) else []) if o is not None][
            :MAX_OPERATIONS
        ],
    )
