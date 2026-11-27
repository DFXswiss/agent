"""Task, session and operation routes of the hub fake as the hub runs them (`agent_hub.tasks.service` `close` and
`control`, `tasks.read.get_session`, `operations.read`; 03-api.md sections 4.4, 4.6 and 4.7):

- `POST /v1/tasks/{task}/close {}` (`task:write`; unknown or unreadable `404`, read-only or archived `403
  forbidden`, any field `400 request_invalid`): the task becomes `closed`, its live sessions with a device `stopping`,
  without one `stopped`; closing again answers the closed row;
- `GET /v1/tasks/{task}/operations?before&limit` (`task:read`, `limit` 1-200, any other parameter `400`), newest first
  with `next_before`; `GET /v1/operations/{id}` with its transitions;
- `GET /v1/sessions/{id}` (`task:read`): row, transitions, usage, VM, children;
- `POST /v1/sessions/{id}/control {action: interrupt|stop}` (`task:write`): recorded as the device hint; `stop`
  without a device ends the session at once.

The scope events route of the hub fake filters by `session` and `ref` itself (`HubFake.r_events`).

Helpers seed sessions, operations and device-written events (sealed with the workgroup key, signed by the device).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from ..hubfake import HubFake, Json, Request, User, now_ms_text

LIVE = ("starting", "working", "waiting", "ready")


@dataclass
class Sessions:
    rows: dict[str, Json] = field(default_factory=dict)
    operations: dict[str, Json] = field(default_factory=dict)
    controls: list[tuple[str, str]] = field(default_factory=list)


def state_of(hub: HubFake) -> Sessions:
    found = getattr(hub, "stage_b_sessions", None)
    if found is None:
        found = Sessions()
        setattr(hub, "stage_b_sessions", found)  # noqa: B010
    return cast(Sessions, found)


def add_session(hub: HubFake, task: str, device: str | None = None, state: str = "working") -> str:
    row = hub.tasks[task]["task"]
    session = uuid7()
    state_of(hub).rows[session] = {
        "session_id": session,
        "task_id": task,
        "org_id": row["org_id"],
        "wg_id": row["wg_id"],
        "owner_id": row["owner_id"],
        "device_id": device,
        "vm_id": None,
        "parent_session_id": None,
        "depth": 0,
        "state": state,
        "state_reason": None,
        "created_at": now_ms_text(),
        "first_working_at": None,
        "ended_at": None,
    }
    return session


def add_operation(hub: HubFake, task: str, session: str, kind: str = "github.create_pr") -> str:
    row = hub.tasks[task]["task"]
    operation = uuid7()
    state_of(hub).operations[operation] = {
        "operation": {
            "operation_id": operation,
            "org_id": row["org_id"],
            "wg_id": row["wg_id"],
            "task_id": task,
            "session_id": session,
            "type": kind,
            "state": "succeeded",
            "error_code": None,
            "created_at": now_ms_text(),
        },
        "transitions": [{"seq": 1, "state": "succeeded", "error_code": None, "at": now_ms_text(), "by_id": None}],
    }
    return operation


def device_event(hub: HubFake, user: User, device: str, kind: str, body: object, **place: str) -> Json:
    """An event of the workgroup `place['wg']` written by `device` (the key from `user`'s wrap)."""
    wg = place["wg"]
    org = cast(str, hub.org_of_wg(wg))
    log = hub.orgs[org].log
    key = hub.org_key(org, user, WG, wg)
    header = new_header(
        id=uuid7(),
        org=org,
        scope=WG,
        scope_id=wg,
        epoch=log.state.epoch(WG, wg),
        author=device,
        kind=kind,
        ts=now_ms_text(),
        lh=log.head.seq,
        **place,
    )
    event = seal_event(key, header, body_json(body), hub.platform_devices[device].sig)
    hub.store_event(wg, event)
    return hub.events[wg][-1]


def _right(hub: HubFake, request: Request, wg: str, write: bool) -> tuple[int, Any] | None:
    org = hub.org_of_wg(wg)
    state = hub.orgs[org].log.state if org is not None else None
    user = cast(str, request.user)
    if state is None or not state.can_read(user, WG, wg):
        return 404, {"error": "not_found"}
    if write and not state.can_write_wg(user, wg):
        return 403, {"error": "forbidden"}
    return None


def install(hub: HubFake) -> None:
    found = state_of(hub)

    def close(request: Request, body: Any, task: str) -> tuple[int, Any]:
        stored = hub.tasks.get(task)
        if stored is None:
            return 404, {"error": "not_found"}
        refused = _right(hub, request, stored["task"]["wg_id"], write=True)
        if refused is not None:
            return refused
        if body != {}:
            return 400, {"error": "request_invalid"}
        row = stored["task"]
        if row["state"] != "closed":
            row |= {"state": "closed", "state_reason": None, "closed_at": now_ms_text()}
            for session in found.rows.values():
                if session["task_id"] == task and session["state"] in LIVE:
                    session["state"] = "stopping" if session["device_id"] else "stopped"
        return 200, {"task": row}

    def operations(request: Request, body: Any, task: str) -> tuple[int, Any]:
        stored = hub.tasks.get(task)
        if stored is None:
            return 404, {"error": "not_found"}
        refused = _right(hub, request, stored["task"]["wg_id"], write=False)
        if refused is not None:
            return refused
        limit = request.query.get("limit", "50")
        if set(request.query) - {"before", "limit"} or not limit.isdigit() or not 1 <= int(limit) <= 200:
            return 400, {"error": "request_invalid"}
        rows = [item["operation"] for item in found.operations.values() if item["operation"]["task_id"] == task]
        rows.reverse()
        start = int(request.query.get("before", "0"))
        page = rows[start : start + int(limit)]
        more = start + int(limit) < len(rows)
        return 200, {"operations": page, "next_before": str(start + int(limit)) if more else None}

    def operation(request: Request, body: Any, operation: str) -> tuple[int, Any]:
        stored = found.operations.get(operation)
        if stored is None or _right(hub, request, stored["operation"]["wg_id"], write=False) is not None:
            return 404, {"error": "not_found"}
        return 200, stored

    def session(request: Request, body: Any, session: str) -> tuple[int, Any]:
        row = found.rows.get(session)
        if row is None or _right(hub, request, row["wg_id"], write=False) is not None:
            return 404, {"error": "not_found"}
        children = [s["session_id"] for s in found.rows.values() if s["parent_session_id"] == session]
        transitions = [{"seq": 1, "state": row["state"], "reason": None, "at": now_ms_text(), "received_at": None}]
        return 200, {"session": row, "transitions": transitions, "usage": None, "vm": None, "children": children}

    def control(request: Request, body: Any, session: str) -> tuple[int, Any]:
        row = found.rows.get(session)
        if row is None:
            return 404, {"error": "not_found"}
        refused = _right(hub, request, row["wg_id"], write=True)
        if refused is not None:
            return refused
        if not isinstance(body, dict) or set(body) != {"action"} or body["action"] not in ("interrupt", "stop"):
            return 400, {"error": "request_invalid"}
        found.controls.append((session, body["action"]))
        if body["action"] == "stop" and row["device_id"] is None and row["state"] in LIVE:
            row["state"] = "stopped"
        return 200, {}

    hub.add_route("POST", r"/v1/tasks/(?P<task>[0-9a-f-]+)/close", close)
    hub.add_route("GET", r"/v1/tasks/(?P<task>[0-9a-f-]+)/operations", operations)
    hub.add_route("GET", r"/v1/operations/(?P<operation>[0-9a-f-]+)", operation)
    hub.add_route("GET", r"/v1/sessions/(?P<session>[0-9a-f-]+)", session)
    hub.add_route("POST", r"/v1/sessions/(?P<session>[0-9a-f-]+)/control", control)
