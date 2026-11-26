"""Usage, audit, operations and metrics of one workgroup for devices (KOR-53, ADR-095 section 4; 03-api.md section 6.1).

`GET /v1/device/workgroups/{wg}/usage|audit|operations|metrics` (right `device:wg`: the device is assigned to the
workgroup; an unknown or foreign workgroup is `404 not_found`). They serve the session read tools `get_usage`,
`list_audit`, `list_workgroup_operations` and `get_metrics` (03-api.md section 8.1); the runner decides which person
may read the workgroup (ADR-089) and checks every row again. The hub only reads existing tables - plaintext metadata
(IDs, numbers, states, times, audit codes), never content - and changes no audit or billing logic.

- **Period.** `since`/`until` (ISO-8601 UTC with milliseconds): `until` defaults to now, `since` is required for usage
  and metrics and defaults to `MAX_DAYS` before `until` for audit and operations (no list or count spans more);
  `since < until` and at most `MAX_DAYS` apart, else `400 request_invalid` with the pointer.
- **Usage** (`by` `session` or `task`, default `task`): the sessions of the workgroup **started** in the period, each
  with its whole counters (`hub.usage_counter`), its charged amount (`hub.charge`, any time), its runtime (creation to
  end, or to now while it runs) and its operation counts (all, and those that ended `failed`, `denied`, `rejected` or
  `expired`); by task the sums per task (the page's tasks are chosen first, then only their sessions are summed). Rows
  newest first (sessions by creation, tasks by the task's creation), at most `limit` (default 50, max 200) with the
  cursor `next_before` of `agent_hub.device.tasks`; `total` sums the whole period on the first page (null with
  `before`, so paging does not aggregate the period again).
- **Audit**: the rows of the workgroup (`wg_id` = the workgroup, in its organization) newest first by `seq`, filtered
  by period, action (the action or a dotted group, as `agent_hub.audit.query`), task and session; the cursor of
  `agent_hub.audit.query`. Rows as the hash covers them. `group_by=action`: the counts per action and result of the
  filtered rows instead (largest first, at most `LIST_MAX` groups, `truncated`).
- **Operations**: the operation rows of every task of the workgroup newest first (`created_at DESC, id DESC`),
  filtered by period, type and state; `group_by=type`: the counts per type, state and error code instead.
- **Metrics**: the series of `agent_hub.monitoring.read.series` for a session, workgroup VM or work VM of the workgroup
  (any other entity `404 not_found`), at most `MAX_POINTS` points.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit import query as audit_query
from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.device.access import DeviceView
from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.errors import HubError
from agent_hub.monitoring import read as monitoring

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 200
MAX_DAYS = 92
MAX_POINTS = 1000
BY = ("task", "session")
ENTITY_KINDS = ("session", "vm", "workvm")
FAILED_STATES = ("failed", "denied", "rejected", "expired")
OPERATION_STATES = frozenset(
    {"received", "rejected", "accepted", "duplicate", "denied", "executing", "succeeded", "failed", "expired"}
)
FIGURES = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_micro_usd",
    "charged_micro_usd",
    "runtime_ms",
    "operations",
    "failed_operations",
)
_SESSIONS = """
WITH s AS (
  SELECT s.id, s.task_id, s.parent_session_id, s.depth, s.state, s.model_id, s.created_at, s.ended_at,
    coalesce(u.requests, 0) AS requests, coalesce(u.input_tokens, 0) AS input_tokens,
    coalesce(u.output_tokens, 0) AS output_tokens, coalesce(u.cache_read_tokens, 0) AS cache_read_tokens,
    coalesce(u.cache_write_tokens, 0) AS cache_write_tokens, coalesce(u.cost_micro_usd, 0) AS cost_micro_usd,
    (SELECT coalesce(sum(c.amount_micro_usd), 0) FROM hub.charge c WHERE c.session_id = s.id) AS charged_micro_usd,
    (extract(epoch FROM coalesce(s.ended_at, %(now)s) - s.created_at) * 1000)::bigint AS runtime_ms,
    (SELECT count(*) FROM hub.operation o WHERE o.session_id = s.id) AS operations,
    (SELECT count(*) FROM hub.operation o WHERE o.session_id = s.id AND o.state = ANY(%(failed)s)) AS failed_operations
  FROM hub.session s LEFT JOIN hub.usage_counter u ON u.session_id = s.id
  WHERE s.wg_id = %(wg)s AND s.created_at >= %(since)s AND s.created_at < %(until)s{only}
)
"""
_SUMS = ", ".join(f"coalesce(sum({name}), 0)::bigint" for name in FIGURES)


@dataclass(frozen=True)
class Period:
    since: datetime
    until: datetime


def _invalid(name: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [f"/{name}"]})


def _time(params: Mapping[str, str | None], name: str) -> datetime | None:
    text = params.get(name)
    if text is None:
        return None
    try:
        return parse_time(text)
    except CryptoError:
        raise _invalid(name) from None


def period(params: Mapping[str, str | None], now: datetime, *, since_required: bool) -> Period:
    """The checked period of a request (module docstring)."""
    since, until = _time(params, "since"), _time(params, "until")
    end = now if until is None else until
    if since is None:
        if since_required:
            raise _invalid("since")
        since = end - timedelta(days=MAX_DAYS)
    elif since >= end or end - since > timedelta(days=MAX_DAYS):
        raise _invalid("since")
    return Period(since, end)


def _workgroup(connection: Connection, view: DeviceView, wg: UUID) -> UUID:
    """The organization of an assigned workgroup (`404 not_found` otherwise)."""
    row = connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg,)).fetchone()
    if row is None or not view.reads_wg(wg):
        raise HubError("not_found")
    return UUID(str(row[0]))


def _figures(values: tuple[Any, ...]) -> dict[str, int]:
    return {name: int(value) for name, value in zip(FIGURES, values, strict=True)}


def usage(
    connection: Connection,
    view: DeviceView,
    wg: UUID,
    when: Period,
    *,
    by: str,
    before: str | None,
    limit: int,
    now: datetime,
) -> dict[str, Any]:
    """The usage of the workgroup in the period (module docstring)."""
    _workgroup(connection, view, wg)
    params: dict[str, Any] = {"wg": wg, "since": when.since, "until": when.until, "now": now}
    params.update(failed=list(FAILED_STATES), limit=limit + 1)
    every = _SESSIONS.format(only="")
    total = None
    cursor = ""
    if before is None:
        counted = connection.execute(
            f"{every} SELECT count(*), count(DISTINCT task_id), {_SUMS} FROM s", params
        ).fetchall()[0]
        total = {"sessions": int(counted[0]), "tasks": int(counted[1]), **_figures(counted[2:])}
    else:
        params["at"], params["id"] = decode_cursor(before)
        cursor = " AND (created_at, id) < (%(at)s, %(id)s)"
    if by == "session":
        found = connection.execute(
            f"{every} SELECT id, task_id, parent_session_id, depth, state, model_id, created_at, ended_at,"
            f" {', '.join(FIGURES)} FROM s WHERE true{cursor} ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
            params,
        ).fetchall()
        rows = [
            {
                "session_id": str(item[0]),
                "task_id": str(item[1]),
                "parent_session_id": None if item[2] is None else str(item[2]),
                "depth": item[3],
                "state": item[4],
                "model_id": item[5],
                "created_at": format_time(item[6]),
                "ended_at": None if item[7] is None else format_time(item[7]),
                **_figures(item[8:]),
            }
            for item in found[:limit]
        ]
        keys = [(item[6], item[0]) for item in found[:limit]]
    else:
        # the page's tasks first (bounded by the index on the sessions), then the figures of only their sessions
        found = connection.execute(
            "SELECT id, created_at FROM hub.task WHERE id IN (SELECT task_id FROM hub.session WHERE wg_id = %(wg)s"
            f" AND created_at >= %(since)s AND created_at < %(until)s){cursor}"
            " ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
            params,
        ).fetchall()
        params["tasks"] = [item[0] for item in found[:limit]]
        sums = {
            item[0]: item[1:]
            for item in connection.execute(
                f"{_SESSIONS.format(only=' AND s.task_id = ANY(%(tasks)s)')}"
                f" SELECT task_id, count(*), {_SUMS} FROM s GROUP BY task_id",
                params,
            )
        }
        rows = [
            {
                "task_id": str(item[0]),
                "created_at": format_time(item[1]),
                "sessions": int(sums[item[0]][0]),
                **_figures(sums[item[0]][1:]),
            }
            for item in found[:limit]
        ]
        keys = [(item[1], item[0]) for item in found[:limit]]
    return {
        "workgroup_id": str(wg),
        "since": format_time(when.since),
        "until": format_time(when.until),
        "by": by,
        "total": total,
        "rows": rows,
        "next_before": encode_cursor(*keys[-1]) if len(found) > limit else None,
    }


def audit(
    connection: Connection,
    view: DeviceView,
    wg: UUID,
    when: Period,
    *,
    filters: Mapping[str, Any],
    before: str | None,
    limit: int,
    grouped: bool = False,
) -> dict[str, Any]:
    """The audit rows of the workgroup (module docstring); `filters` from `audit_query.parse_filters` without `wg` and
    `actor`; `grouped`: the counts per action and result instead."""
    org = _workgroup(connection, view, wg)
    clauses = ["org_id = %s", "wg_id = %s", "at >= %s", "at < %s"]
    params = list[Any]([org, wg, when.since, when.until])
    for name, value in filters.items():
        if name == "action":
            clauses.append("(action = %s OR left(action, %s) = %s)")
            params.extend([value, len(value) + 1, value + "."])
        else:
            clauses.append(f"{name}_id = %s")
            params.append(value)
    if grouped:
        return _groups(connection, "hub.audit", ("action", "result"), clauses, params)
    if before is not None:
        clauses.append("seq < %s")
        params.append(audit_query.decode_before(before))
    found = connection.execute(
        f"SELECT {audit_query.COLUMNS} FROM hub.audit WHERE {' AND '.join(clauses)} ORDER BY seq DESC LIMIT %s",
        (*params, limit + 1),
    ).fetchall()
    rows = [audit_query.row(record) for record in found[:limit]]
    return {"rows": rows, "next_before": audit_query.encode_before(rows[-1]["seq"]) if len(found) > limit else None}


def operations(
    connection: Connection,
    view: DeviceView,
    wg: UUID,
    when: Period,
    *,
    op_type: str | None,
    state: str | None,
    before: str | None,
    limit: int,
    grouped: bool = False,
) -> dict[str, Any]:
    """The operations of every task of the workgroup (module docstring); `grouped`: the counts per type, state and
    error code instead."""
    _workgroup(connection, view, wg)
    clauses = ["wg_id = %s", "created_at >= %s", "created_at < %s"]
    params = list[Any]([wg, when.since, when.until])
    for column, value in (("type", op_type), ("state", state)):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    if grouped:
        return _groups(connection, "hub.operation", ("type", "state", "error_code"), clauses, params)
    if before is not None:
        at, op_id = decode_cursor(before)
        clauses.append("(created_at, id) < (%s, %s)")
        params.extend([at, op_id])
    found = connection.execute(
        "SELECT id, task_id, session_id, type, state, error_code, created_at, finished_at FROM hub.operation"
        f" WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC LIMIT %s",
        (*params, limit + 1),
    ).fetchall()
    rows = [
        {
            "operation_id": str(item[0]),
            "task_id": str(item[1]),
            "session_id": str(item[2]),
            "type": item[3],
            "state": item[4],
            "error_code": item[5],
            "created_at": format_time(item[6]),
            "finished_at": None if item[7] is None else format_time(item[7]),
        }
        for item in found[:limit]
    ]
    last = found[limit - 1] if len(found) > limit else None
    return {"operations": rows, "next_before": None if last is None else encode_cursor(last[6], last[0])}


def _groups(
    connection: Connection, table: str, columns: tuple[str, ...], clauses: list[str], params: list[Any]
) -> dict[str, Any]:
    """The counts of the filtered rows per value of `columns`, largest first (ties by the values), at most
    `LIST_MAX` groups (`truncated` when there are more)."""
    names = ", ".join(columns)
    found = connection.execute(
        f"SELECT {names}, count(*) FROM {table} WHERE {' AND '.join(clauses)} GROUP BY {names}"
        f" ORDER BY count(*) DESC, {names} LIMIT %s",
        (*params, LIST_MAX + 1),
    ).fetchall()
    groups = [{**dict(zip(columns, item[:-1], strict=True)), "count": int(item[-1])} for item in found[:LIST_MAX]]
    return {"groups": groups, "truncated": len(found) > LIST_MAX}


def metrics(
    connection: Connection, view: DeviceView, wg: UUID, params: Mapping[str, str | None], now: datetime
) -> dict[str, Any]:
    """The series of an entity of the workgroup (module docstring)."""
    _workgroup(connection, view, wg)
    when = period(params, now, since_required=True)
    if params.get("entity_kind") not in ENTITY_KINDS:
        raise _invalid("entity_kind")
    query = monitoring.parse_series(
        {
            "entity_kind": params.get("entity_kind"),
            "entity_id": params.get("entity_id"),
            "metric": params.get("metric"),
            "from": format_time(when.since),
            "to": format_time(when.until),
            "step": params.get("step"),
        },
        now,
    )
    if query.points > MAX_POINTS:
        raise _invalid("step")
    if monitoring.entity_wg(connection, query.entity_kind, query.entity_id) != wg:
        raise HubError("not_found")
    return {
        "entity_kind": query.entity_kind,
        "entity_id": query.entity_id,
        "metric": query.metric,
        **monitoring.series(connection, query, now),
    }
