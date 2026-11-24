"""Monitoring reads: who sees which entity, the overview and the time series (03-api.md section 4.11,
08-monitoring.md section 6).

**Visibility.** An org member sees the devices of the org: every active device member, i.e. the org's own devices and
the platform devices assigned to its workgroups (`agent_hub.workgroups.read.org_devices`). A platform device is shared
between orgs, but only the device sample is shown for it - VMs and sessions are shown only for the workgroups the
member reads (`wg:read`), so another org's VMs and sessions on a shared device never appear. VMs: every VM row of a
readable workgroup except `deleted` ones (a pool VM has no workgroup and is not shown). Sessions: the sessions of
readable workgroups that have not ended or ended less than `SESSION_GRACE` ago (the 15 minutes the Prometheus series
of an ended session stay, 08-monitoring.md section 3): at most `SESSIONS_MAX`, the most recently active first (the hub
time of the session's newest state transition, else its creation; ties by ID), with `sessions_total` (all such
sessions) and `truncated` (ADR-086 section 2). The devices are those of the org's device list (at most its cap,
`devices_truncated` when it cut them).
Work VMs (KOR-29): the work VMs of those sessions
with a sample within `SESSION_GRACE` (the hub keeps no row per work VM; a work VM that was torn down disappears 15
minutes after its last sample), under the device that sampled them, with the session's task and workgroup.

**Overview.** Every entity carries its newest raw sample (`latest`, null without one within the raw retention) and
`stale`: true when there is no sample or the newest one is more than `STALE_SECONDS` (60 s) older than the hub clock
(06-web.md greys such values with their age). Devices carry presence, capacity and `capacity_low` (< 10 % free VM
slots), sessions their `usage_counter` row (null before the first `usage` item).

**Series.** `entity_kind`, `entity_id`, `metric`, `from`, `to`, `step` (seconds). The range `[from, to)` is split into
`ceil((to - from) / step)` (at most `MAX_POINTS`) buckets starting at `from`; each point is `[bucket start, value]`
with null for a bucket without data. A range that starts within the raw retention (48 h) is read from the raw samples
(`step` >= 15 s), an older one from the 5-minute rollups (`step` >= 300 s). Gauges are averaged over a bucket (rollups
weighted by their sample count); counters (`net_rx_bytes`, `net_tx_bytes`), `uptime_seconds` and `reachable` (1/0)
take the newest value in the bucket. The rights are those of the overview for the entity: a device of an org the
caller is a member of, a VM or session of a workgroup the caller reads, a work VM whose session (of its newest sample)
is of a workgroup the caller reads; anything else is `404 not_found`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.errors import HubError
from agent_hub.monitoring.rollup import BUCKET_SECONDS, GAUGES, RAW_RETENTION
from agent_hub.orgs.rights import wg_access
from agent_hub.stream.sse import readable_scopes
from agent_hub.workgroups.read import device_window, org_devices

Connection = psycopg.Connection[Any]
STALE_SECONDS = 60
SESSION_GRACE = timedelta(minutes=15)
SESSIONS_MAX = 2000
RAW_MIN_STEP = 15
ROLLUP_MIN_STEP = BUCKET_SECONDS
MAX_POINTS = 20000
SAMPLE_COLUMNS = (
    "cpu_ratio",
    "mem_used_bytes",
    "mem_limit_bytes",
    "disk_used_bytes",
    "disk_limit_bytes",
    "net_rx_bytes",
    "net_tx_bytes",
    "uptime_seconds",
    "reachable",
    "state",
    "free_vm_slots",
    "free_mem_bytes",
)
USAGE_COLUMNS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "thinking_tokens",
    "cost_micro_usd",
    "cost_basis",
    "context_used_tokens",
    "context_window_tokens",
    "last_provider",
    "updated_at",
)
METRICS = frozenset(name for name in SAMPLE_COLUMNS if name != "state")
ENTITY_KINDS = ("device", "vm", "session", "workvm")
_VM_ID_RE = re.compile(r"vm-[0-9a-f]{8}")
_WORKVM_ID_RE = re.compile(r"wvm-[0-9a-f]{8}")
_STEP_RE = re.compile(r"[1-9][0-9]{1,6}")


# --- samples ---


def sample_json(ts: datetime, values: Sequence[Any]) -> dict[str, Any]:
    """A stored sample (`ts` and the values of `SAMPLE_COLUMNS`) as `objects.json#/$defs/metric_sample`."""
    found: dict[str, Any] = {"ts": format_time(ts)}
    for name, value in zip(SAMPLE_COLUMNS, values, strict=True):
        found[name] = float(value) if name == "cpu_ratio" and value is not None else value
    return found


def latest_samples(connection: Connection, kind: str, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The newest raw sample of every entity of `kind` among `ids` that has one."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = connection.execute(
        f"SELECT e.id, s.ts, {', '.join(f's.{name}' for name in SAMPLE_COLUMNS)}"
        " FROM unnest(%s::text[]) AS e(id) CROSS JOIN LATERAL (SELECT * FROM hub.metric_sample m"
        " WHERE m.entity_kind = %s AND m.entity_id = e.id ORDER BY m.ts DESC LIMIT 1) s",
        (wanted, kind),
    ).fetchall()
    return {str(row[0]): sample_json(row[1], row[2:]) for row in rows}


def is_stale(latest: Mapping[str, Any] | None, now: datetime) -> bool:
    """No sample, or the newest one is more than `STALE_SECONDS` old."""
    if latest is None:
        return True
    return (now - parse_time(latest["ts"])).total_seconds() > STALE_SECONDS


# --- visibility ---


@dataclass(frozen=True)
class OrgView:
    """What one member sees of an org's monitoring (module docstring)."""

    devices: list[dict[str, Any]]
    vms: list[tuple[str, str, str, str]]
    """`(vm_id, device_id, wg_id, state)`, newest first."""
    sessions: list[tuple[str, str, str, str]]
    """`(session_id, task_id, wg_id, state)`, the most recently active first (at most `SESSIONS_MAX`)."""
    sessions_total: int
    """The number of sessions shown without the cap."""
    devices_truncated: bool = False
    """The device list's cap cut the devices."""
    workvms: list[tuple[str, str, str]] = field(default_factory=list)
    """`(workvm_id, device_id, session_id)` of the visible sessions' work VMs (KOR-29), newest sample first."""


def readable_wgs(connection: Connection, org: UUID, user: UUID) -> list[str]:
    """The workgroups of the org the user reads (`404 not_found` when the user is not a member)."""
    scopes = readable_scopes(connection, org, user)
    if scopes is None:
        raise HubError("not_found")
    return sorted(str(scope) for scope in scopes if scope != org)


def org_view(connection: Connection, org: UUID, user: UUID, now: datetime, online_seconds: int) -> OrgView:
    """The devices, VMs and sessions of the org the member sees."""
    wgs = readable_wgs(connection, org, user)
    listed = org_devices(connection, org, now, online_seconds, user=user, window=device_window(None, None))
    devices = listed["devices"]
    vms = connection.execute(
        "SELECT id, device_id, wg_id, state FROM hub.vm WHERE wg_id = ANY(%s::uuid[]) AND state <> 'deleted'"
        " ORDER BY created_at DESC, id DESC",
        (wgs,),
    ).fetchall()
    # an ended session stays while a work VM it requested still sends samples (the task VM other sessions use)
    sessions = connection.execute(
        "SELECT s.id, s.task_id, s.wg_id, s.state, count(*) OVER () FROM hub.session s WHERE s.org_id = %s"
        " AND s.wg_id = ANY(%s::uuid[]) AND (s.ended_at IS NULL OR s.ended_at > %s OR s.id IN (SELECT session_id"
        " FROM hub.metric_sample WHERE entity_kind = 'workvm' AND ts > %s))"
        " ORDER BY COALESCE((SELECT t.received_at FROM hub.session_transition t WHERE t.session_id = s.id"
        " ORDER BY t.seq DESC LIMIT 1), s.created_at) DESC, s.id DESC LIMIT %s",
        (org, wgs, now - SESSION_GRACE, now - SESSION_GRACE, SESSIONS_MAX),
    ).fetchall()
    workvms = connection.execute(
        "SELECT entity_id, device_id, session_id FROM (SELECT DISTINCT ON (entity_id) entity_id, device_id, session_id,"
        " ts FROM hub.metric_sample WHERE entity_kind = 'workvm' AND session_id = ANY(%s::uuid[]) AND ts > %s"
        " ORDER BY entity_id, ts DESC) w ORDER BY ts DESC, entity_id",
        ([str(row[0]) for row in sessions], now - SESSION_GRACE),
    ).fetchall()
    return OrgView(
        devices=devices,
        vms=[(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in vms],
        sessions=[(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in sessions],
        workvms=[(str(row[0]), str(row[1]), str(row[2])) for row in workvms],
        sessions_total=int(sessions[0][4]) if sessions else 0,
        devices_truncated=listed.get("truncated", False),
    )


def usage_rows(connection: Connection, sessions: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The `usage_counter` rows of the sessions that have one, as `objects.json#/$defs/usage`."""
    wanted = sorted(set(sessions))
    if not wanted:
        return {}
    rows = connection.execute(
        f"SELECT session_id, {', '.join(USAGE_COLUMNS)} FROM hub.usage_counter WHERE session_id = ANY(%s::uuid[])",
        (wanted,),
    ).fetchall()
    found = {}
    for row in rows:
        usage = dict(zip(USAGE_COLUMNS, row[1:], strict=True))
        usage["updated_at"] = format_time(usage["updated_at"])
        found[str(row[0])] = usage
    return found


def overview(connection: Connection, org: UUID, user: UUID, now: datetime, online_seconds: int) -> dict[str, Any]:
    """`GET /v1/monitoring/overview` (module docstring)."""
    view = org_view(connection, org, user, now, online_seconds)
    device_latest = latest_samples(connection, "device", (item["device_id"] for item in view.devices))
    vm_latest = latest_samples(connection, "vm", (vm[0] for vm in view.vms))
    session_latest = latest_samples(connection, "session", (session[0] for session in view.sessions))
    workvm_latest = latest_samples(connection, "workvm", (workvm[0] for workvm in view.workvms))
    usage = usage_rows(connection, (session[0] for session in view.sessions))
    devices = []
    for item in view.devices:
        latest = device_latest.get(item["device_id"])
        devices.append(
            {
                "device_id": item["device_id"],
                "online": item["online"],
                "capacity": item["capacity"],
                "capacity_low": item["capacity_low"],
                "latest": latest,
                "stale": is_stale(latest, now),
            }
        )
    vms = []
    for vm_id, device_id, wg_id, state in view.vms:
        latest = vm_latest.get(vm_id)
        vms.append(
            {
                "vm_id": vm_id,
                "device_id": device_id,
                "wg_id": wg_id,
                "state": state,
                "latest": latest,
                "stale": is_stale(latest, now),
            }
        )
    sessions = []
    for session_id, task_id, wg_id, state in view.sessions:
        latest = session_latest.get(session_id)
        sessions.append(
            {
                "session_id": session_id,
                "task_id": task_id,
                "wg_id": wg_id,
                "state": state,
                "latest": latest,
                "usage": usage.get(session_id),
                "stale": is_stale(latest, now),
            }
        )
    of_session = {session_id: (task_id, wg_id) for session_id, task_id, wg_id, _ in view.sessions}
    workvms = []
    for workvm_id, device_id, session_id in view.workvms:
        latest = workvm_latest[workvm_id]  # the work VM was found by a sample within the grace
        task_id, wg_id = of_session[session_id]
        workvms.append(
            {
                "workvm_id": workvm_id,
                "device_id": device_id,
                "session_id": session_id,
                "task_id": task_id,
                "wg_id": wg_id,
                "state": latest["state"],
                "latest": latest,
                "stale": is_stale(latest, now),
            }
        )
    return {
        "devices": devices,
        "vms": vms,
        "sessions": sessions,
        "workvms": workvms,
        "sessions_total": view.sessions_total,
        "truncated": view.sessions_total > len(sessions),
        "devices_truncated": view.devices_truncated,
    }


# --- series ---


@dataclass(frozen=True)
class SeriesQuery:
    """A checked series request."""

    entity_kind: str
    entity_id: str
    metric: str
    start: datetime
    end: datetime
    step: int

    @property
    def points(self) -> int:
        return math.ceil((self.end - self.start).total_seconds() / self.step)


def _invalid(pointer: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [pointer]})


def _time(text: str | None, pointer: str) -> datetime:
    try:
        return parse_time(text)
    except CryptoError:
        raise _invalid(pointer) from None


def _entity_id(kind: str, text: str | None) -> str:
    if text is not None:
        if kind == "vm" and _VM_ID_RE.fullmatch(text):
            return text
        if kind == "workvm" and _WORKVM_ID_RE.fullmatch(text):
            return text
        if kind not in ("vm", "workvm"):
            try:
                if str(UUID(text)) == text:
                    return text
            except ValueError:
                pass
    raise _invalid("/entity_id")


def parse_series(params: Mapping[str, str | None], now: datetime) -> SeriesQuery:
    """The series request (`400 request_invalid` with the pointer of the first bad parameter)."""
    kind = params.get("entity_kind")
    if kind not in ENTITY_KINDS:
        raise _invalid("/entity_kind")
    entity_id = _entity_id(kind, params.get("entity_id"))
    metric = params.get("metric")
    if metric not in METRICS:
        raise _invalid("/metric")
    start = _time(params.get("from"), "/from")
    end = _time(params.get("to"), "/to")
    if end <= start:
        raise _invalid("/to")
    text = params.get("step")
    if text is None or _STEP_RE.fullmatch(text) is None:
        raise _invalid("/step")
    step = int(text)
    minimum = RAW_MIN_STEP if start >= now - RAW_RETENTION else ROLLUP_MIN_STEP
    query = SeriesQuery(kind, entity_id, metric, start, end, step)
    if step < minimum or query.points > MAX_POINTS:
        raise _invalid("/step")
    return query


def entity_visible(connection: Connection, user: UUID, kind: str, entity_id: str) -> bool:
    """The overview of some org shows the entity to the user (module docstring)."""
    if kind == "device":
        row = connection.execute(
            "SELECT 1 FROM hub.org_member d JOIN hub.org_member u ON u.org_id = d.org_id AND u.principal_id = %s"
            " AND u.removed_seq IS NULL AND u.role IN ('admin', 'member')"
            " WHERE d.principal_id = %s AND d.kind = 'device' AND d.removed_seq IS NULL LIMIT 1",
            (user, entity_id),
        ).fetchone()
        return row is not None
    if kind == "workvm":
        found = connection.execute(
            "SELECT s.wg_id FROM hub.session s WHERE s.id = (SELECT session_id FROM hub.metric_sample"
            " WHERE entity_kind = 'workvm' AND entity_id = %s ORDER BY ts DESC LIMIT 1)",
            (entity_id,),
        ).fetchone()
    else:
        table = "hub.vm" if kind == "vm" else "hub.session"
        found = connection.execute(f"SELECT wg_id FROM {table} WHERE id = %s", (entity_id,)).fetchone()
    return found is not None and found[0] is not None and wg_access(connection, found[0], user) is not None


def _aggregate(metric: str, rollup: bool) -> str:
    if metric in GAUGES:
        if rollup:
            return (
                f"sum({metric}_avg::float8 * samples) / NULLIF(sum(samples) FILTER (WHERE {metric}_avg IS NOT NULL), 0)"
            )
        return f"avg({metric})::float8"
    value = "reachable::int" if metric == "reachable" else metric
    return f"(array_agg({value} ORDER BY ts DESC) FILTER (WHERE {value} IS NOT NULL))[1]"


def series(connection: Connection, query: SeriesQuery, now: datetime) -> dict[str, Any]:
    """`GET /v1/monitoring/series` for a checked query of a visible entity (module docstring)."""
    rollup = query.start < now - RAW_RETENTION
    table = "hub.metric_rollup" if rollup else "hub.metric_sample"
    value = _aggregate(query.metric, rollup)
    rows = connection.execute(
        f"SELECT floor(extract(epoch FROM ts - %(start)s) / %(step)s)::bigint AS bucket, {value}"
        f" FROM {table} WHERE entity_kind = %(kind)s AND entity_id = %(id)s AND ts >= %(start)s AND ts < %(end)s"
        " GROUP BY bucket",
        {"start": query.start, "end": query.end, "step": query.step, "kind": query.entity_kind, "id": query.entity_id},
    ).fetchall()
    values = {int(row[0]): row[1] for row in rows}
    points = []
    for index in range(query.points):
        moment = query.start + timedelta(seconds=index * query.step)
        points.append([format_time(moment), values.get(index)])
    return {"points": points}
