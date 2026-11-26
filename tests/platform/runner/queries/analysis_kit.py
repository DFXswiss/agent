"""Test kit of the process analysis reads (KOR-53, ADR-095 sections 3 and 4): the hub's device routes
`GET /v1/device/workgroups/{wg}/usage|audit|operations|metrics` installed on the rig's hub next to `kit.TaskHub`.

`AnalysisHub` answers every route from `answers[resource](wg, query)` (default: contract-valid rows of the asked
workgroup), checks each request against `api/device-workgroup-<resource>.request.json` and each answer against the
response contract (violations fail the rig), answers `404 not_found` for a workgroup the device is not assigned to,
and sends `raw[resource]` unchecked (a hub that breaks the contract). The transport failure of `TaskHub.unreachable`
covers these routes too. `queries` keeps `(wg, resource, query)` of every request.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent_runner.queries.analysis_api import FIGURES
from agent_runner.sessions.records import time_text

from ..reconcile.rig import Env
from .kit import ALICE, ORG, TASK, TASK_B, WG, WG_ONE

Json = dict[str, Any]
SINCE = "2026-11-14T12:00:00.000Z"
UNTIL = "2026-11-21T12:00:00.000Z"
S1 = "01930005-0000-7000-8000-000000000101"
S2 = "01930005-0000-7000-8000-000000000102"
OP1 = "01930007-0000-7000-8000-000000000101"
OP2 = "01930007-0000-7000-8000-000000000102"
CURSOR = "MjAyNi0xMS0yMQ_abc"
_ROUTE = re.compile(r"/v1/device/workgroups/([^/]+)/(usage|audit|operations|metrics)")


def figures(n: int) -> Json:
    return {name: n * 10 + index for index, name in enumerate(FIGURES)}


def usage_row(by: str, n: int) -> Json:
    stamp = f"2026-11-20T12:{n:02d}:00.000Z"
    if by == "task":
        return {"task_id": (TASK, TASK_B)[n % 2], "created_at": stamp, "sessions": n + 1, **figures(n)}
    return {
        "session_id": (S1, S2)[n % 2],
        "task_id": TASK,
        "parent_session_id": None if n % 2 == 0 else S1,
        "depth": n % 2,
        "state": "stopped",
        "model_id": "deepseek/deepseek-v4.1-flash-1017" if n % 2 == 0 else None,
        "created_at": stamp,
        "ended_at": None if n % 2 == 0 else stamp,
        **figures(n),
    }


def total() -> Json:
    return {"sessions": 2, "tasks": 1, **figures(3)}


def usage(wg: str, query: dict[str, str]) -> Json:
    by = query.get("by", "task")
    return {
        "workgroup_id": wg,
        "since": query["since"],
        "until": query.get("until", time_text(time.time())),
        "by": by,
        "total": None if "before" in query else total(),
        "rows": [usage_row(by, 0), usage_row(by, 1)],
        "next_before": CURSOR,
    }


def audit_row(seq: int, wg: str = WG, **changes: Any) -> Json:
    row = {
        "seq": seq,
        "at": f"2026-11-20T12:00:{seq % 60:02d}.000Z",
        "actor_id": ALICE,
        "actor_kind": "user",
        "action": "operation.received",
        "result": "ok",
        "org_id": ORG,
        "wg_id": wg,
        "task_id": TASK,
        "session_id": S1,
        "device_id": None,
        "target_kind": "operation",
        "target_id": OP1,
        "detail": {"type": "github.push", "attempt": 2, "retry": False, "note": None},
        "event_id": None,
    }
    return {**row, **changes}


def audit(wg: str, query: dict[str, str]) -> Json:
    if "group_by" in query:
        return {"groups": [{"action": "operation.received", "result": "ok", "count": 7}], "truncated": False}
    return {"rows": [audit_row(9, wg), audit_row(8, wg)], "next_before": CURSOR}


def operation_row(op: str = OP1, **changes: Any) -> Json:
    row = {
        "operation_id": op,
        "task_id": TASK,
        "session_id": S1,
        "type": "github.push",
        "state": "failed",
        "error_code": "non_fast_forward",
        "created_at": "2026-11-20T12:00:00.000Z",
        "finished_at": None,
    }
    return {**row, **changes}


def operations(_wg: str, query: dict[str, str]) -> Json:
    if "group_by" in query:
        groups = [
            {"type": "github.push", "state": "failed", "error_code": "non_fast_forward", "count": 3},
            {"type": "github.push", "state": "succeeded", "error_code": None, "count": 1},
        ]
        return {"groups": groups, "truncated": False}
    return {
        "operations": [operation_row(OP1), operation_row(OP2, state="succeeded", error_code=None)],
        "next_before": None,
    }


def metrics(_wg: str, query: dict[str, str]) -> Json:
    return {
        "entity_kind": query["entity_kind"],
        "entity_id": query["entity_id"],
        "metric": query["metric"],
        "points": [["2026-11-20T12:00:00.000Z", 0.5], ["2026-11-20T13:00:00.000Z", None]],
    }


@dataclass
class AnalysisHub:
    env: Env
    answers: dict[str, Callable[[str, dict[str, str]], Json]] = field(
        default_factory=lambda: {"usage": usage, "audit": audit, "operations": operations, "metrics": metrics}
    )
    raw: dict[str, tuple[int, Json]] = field(default_factory=dict)
    queries: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def install(self) -> None:
        hub = self.env.hub
        original = hub.route

        def route(method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
            parts = urlsplit(target)
            matched = _ROUTE.fullmatch(parts.path)
            if method != "GET" or matched is None:
                return original(method, target, body)
            wg, resource = matched.groups()
            query = {name: values[0] for name, values in parse_qs(parts.query).items()}
            self.queries.append((wg, resource, query))
            hub.check(f"api/device-workgroup-{resource}.request.json", query)
            if resource in self.raw:
                return self.raw[resource]
            if wg not in (WG, WG_ONE):
                return 404, {"error": "not_found"}
            answer = self.answers[resource](wg, query)
            hub.check(f"api/device-workgroup-{resource}.response.json", answer)
            return 200, answer

        hub.route = route  # type: ignore[method-assign]

    def last(self) -> tuple[str, str, dict[str, str]]:
        return self.queries[-1]
