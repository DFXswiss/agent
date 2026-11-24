"""The audit guard of write requests (ADR-063 section 1): every `POST`, `PUT`, `PATCH` and `DELETE` that reached its
route leaves at least one committed audit row, and every refused one a committed row with a result other than `ok`.

The pipeline runs the route under `agent_hub.audit.recorder.recording()` and, before the answer leaves the hub, calls
`settle` in a worker thread:

* status < 400 and no committed row of the request: `request.write` (result `ok`). It is the audit of a route of
  `agent_hub.audit.generic_routes.GENERIC` and of a request its handler declared an unchanged repeat
  (`agent_hub.audit.recorder.unchanged`); for anything else it marks a defect, logged and counted in `UNAUDITED` (the
  hub test suite fails a test that produced one).
* status >= 400 and no committed row of the request whose result is not `ok`: `request.refused` in a transaction of its
  own (the route's transaction is rolled back by then), result `denied` (401, 403, 404), `failed` (>= 500) or
  `rejected` (other 4xx).

Both go through the windowed log of `agent_hub.http.readlog` (the first row of a key is written at once, repeats in its
window are counted in `audit.repeat`), so a device that pushes telemetry or retries does not flood the chain.

Both rows carry the request's actor (`Principal.actor_kind`, the agent of an agent principal), the device of a device
principal, the organization, workgroup, task and session IDs the request named (path parameters of those names, else
single query parameters; UUIDv7 text only, never looked up), the last path parameter as target and the detail
`{method, route, status, code}` (`route` is the path template, never the requested path; `code` the error code of the
answer, `ok` for a success). Requests refused before a principal is known and `429` answers are not audited here
(module `agent_hub.http.pipeline`).
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.audit.chain import AuditRow
from agent_hub.audit.generic_routes import GENERIC
from agent_hub.audit.placement import named_ids, target
from agent_hub.audit.recorder import Recording, committed
from agent_hub.auth.keys import Principal
from agent_hub.db.pool import transaction
from agent_hub.errors import ERROR_STATUSES
from agent_hub.http.readlog import ReadLog

LOG = logging.getLogger("agent_hub.writelog")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
UNAUDITED: Counter[str] = Counter()
"""`<METHOD> <route>` of every successful write outside GENERIC that left no row of its own (this process)."""


def write_result(status: int) -> str:
    """The audit result of a write answered with `status`."""
    if status < 400:
        return "ok"
    if status in (401, 403, 404):
        return "denied"
    return "failed" if status >= 500 else "rejected"


def error_code(status: int, body: bytes) -> str:
    """The error code of an answer (`ok` below 400; `unknown` when the body names no code of the closed list)."""
    if status < 400:
        return "ok"
    try:
        found = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return "unknown"
    return found if isinstance(found, str) and found in ERROR_STATUSES else "unknown"


def guard_row(
    principal: Principal,
    *,
    method: str,
    route: str,
    status: int,
    code: str,
    path_params: Mapping[str, Any],
    query: Mapping[str, str],
    at: datetime,
) -> AuditRow:
    """The pipeline's own row of a write request (module docstring)."""
    ids = named_ids(path_params, query)
    kind, target_id = target(path_params, "id")
    agent = principal.attributes.get("agent_id") if principal.kind == "agent" else None
    return AuditRow(
        at=at,
        actor_kind=principal.actor_kind,
        actor_id=principal.principal_id,
        action="request.write" if status < 400 else "request.refused",
        result=write_result(status),
        org_id=ids["org"],
        wg_id=ids["wg"],
        task_id=ids["task"],
        session_id=ids["session"],
        device_id=principal.principal_id if principal.kind == "device" else None,
        target_kind=kind,
        target_id=target_id,
        detail={"method": method, "route": route[:256], "status": status, "code": code},
        agent_id=agent if isinstance(agent, UUID) else None,
    )


def _recorded(connection: psycopg.Connection[Any], recorded: Recording, status: int) -> bool:
    """The request's committed rows record its outcome (any row for a success, a row that is not `ok` for a
    refusal)."""
    rows = committed(connection, recorded.rows)
    if status < 400:
        return bool(rows) or recorded.deferred
    return any(row.result != "ok" for row in rows)


def settle(
    pool: ConnectionPool[psycopg.Connection[Any]],
    log: ReadLog,
    recorded: Recording,
    row: AuditRow,
    *,
    status: int,
    query: str,
) -> bool:
    """Write `row` (of a request answered with `status`, whose query has the fingerprint `query`) through the windowed
    log unless the request's committed rows record its outcome; True when it was written or counted."""
    with transaction(pool) as connection:
        if _recorded(connection, recorded, status):
            return False
    if status < 400 and not recorded.unchanged:
        method, route = str(row.detail["method"]), str(row.detail["route"])
        if (method, route) not in GENERIC:
            UNAUDITED[f"{method} {route}"] += 1
            LOG.error("audit: the successful write %s %s left no audit row of its own", method, route)
    log.admit(row, query)
    return True
