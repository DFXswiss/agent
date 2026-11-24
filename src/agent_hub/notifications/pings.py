"""Durable, content-free session pings (03-api.md sections 4.9, 6.1, 6.2; 02-data-model.md section 2.6; ADR-020).

A new whiteboard entry sent with `notify:true` pings the active sessions in its scope - org level: every session of the
org; workgroup level: of the workgroup; task level: of the task. "Active" means not `stopping`, `stopped` or `failed`:
a stopping session ends without a further turn and would never deliver the notice. `create_whiteboard_pings` writes one
`session_ping` row per session (kind `whiteboard.entry`, the level, `ref` = the entry) in the storing transaction; a
session that has no device yet keeps its row and gets it with its device's state once it is placed.

After the commit `ping_hints` names, for every device that runs one of those sessions, the hint `ping {org_id, wg_id?,
task_id?, level, entry_id}` (one per device; the runner pings its own sessions in scope, 05-runner.md section 9). The
hub never queues hints: an offline device finds the rows in the `pings` section of `GET /v1/device/state`
(`undelivered`) and the runner marks each delivered ping with the outbox audit item `ping.delivered` (outbox area).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.whiteboards.projection import ENTRY, ORG, TASK, BoardEvent

Connection = psycopg.Connection[Any]
INACTIVE = ("stopping", "stopped", "failed")
"""Session states that are not pinged."""
FINAL = ("stopped", "failed")
"""Session states whose undelivered pings are no longer listed."""
STATE_LIMIT = 1000
"""At most this many undelivered pings in one device state (oldest first; the rest follows after delivery)."""


def _scope_clause(level: str) -> str:
    if level == ORG:
        return "org_id = %s"
    return "wg_id = %s AND task_id = %s" if level == TASK else "wg_id = %s"


def _scope_ids(event: BoardEvent) -> tuple[UUID | None, ...]:
    if event.level == ORG:
        return (event.org_id,)
    return (event.wg_id, event.task_id) if event.level == TASK else (event.wg_id,)


def create_whiteboard_pings(connection: Connection, event: BoardEvent) -> int:
    """One `session_ping` row per active session in the scope of the entry; the number of rows."""
    sessions = connection.execute(
        f"SELECT id FROM hub.session WHERE {_scope_clause(event.level)} AND state <> ALL(%s) ORDER BY id",
        (*_scope_ids(event), list(INACTIVE)),
    ).fetchall()
    millis = int(event.received_at.timestamp() * 1000)
    for (session,) in sessions:
        connection.execute(
            "INSERT INTO hub.session_ping (id, session_id, kind, level, ref, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (uuid7(millis), session, ENTRY, event.level, str(event.ref), event.received_at),
        )
    return len(sessions)


def hint_body(level: str, org_id: UUID, wg_id: UUID | None, task_id: UUID | None, entry_id: UUID) -> dict[str, Any]:
    """The body of the `ping` hint (`wg_id` below the org level, `task_id` on the task level)."""
    body: dict[str, Any] = {"org_id": str(org_id), "level": level, "entry_id": str(entry_id)}
    if level != ORG:
        body["wg_id"] = str(wg_id)
    if level == TASK:
        body["task_id"] = str(task_id)
    return body


def ping_hints(connection: Connection, entry_id: UUID) -> list[tuple[UUID, dict[str, Any]]]:
    """`(device, hint body)` for every device with an undelivered ping of the entry, sorted by device."""
    rows = connection.execute(
        "SELECT DISTINCT s.device_id, w.level, w.org_id, w.wg_id, w.task_id FROM hub.session_ping p"
        " JOIN hub.session s ON s.id = p.session_id JOIN hub.whiteboard_entry w ON w.id = %s"
        " WHERE p.kind = %s AND p.ref = %s AND p.delivered_at IS NULL AND s.device_id IS NOT NULL"
        " AND s.state <> ALL(%s) ORDER BY s.device_id",
        (entry_id, ENTRY, str(entry_id), list(INACTIVE)),
    ).fetchall()
    return [(device, hint_body(level, org, wg, task, entry_id)) for device, level, org, wg, task in rows]


def undelivered(connection: Connection, device_id: UUID) -> list[dict[str, Any]]:
    """The undelivered pings of the sessions on the device that have not ended, oldest first."""
    rows = connection.execute(
        "SELECT p.id, p.session_id, p.kind, p.level, p.ref, p.created_at FROM hub.session_ping p"
        " JOIN hub.session s ON s.id = p.session_id WHERE s.device_id = %s AND p.delivered_at IS NULL"
        " AND s.state <> ALL(%s) ORDER BY p.created_at, p.id LIMIT %s",
        (device_id, list(FINAL), STATE_LIMIT),
    ).fetchall()
    return [
        {
            "ping_id": str(ping),
            "session_id": str(session),
            "kind": kind,
            "level": level,
            "ref": ref,
            "created_at": format_time(created_at),
        }
        for ping, session, kind, level, ref, created_at in rows
    ]
