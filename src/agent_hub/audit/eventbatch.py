"""The audit of the session's own events (ADR-063 section 4): one `event.append_batch` row per session and scope
instead of one `event.append` row per event.

Events of `BATCHED_KINDS` - what a running session produces in bulk: output, terminal, tool and hook records, file
changes - are stored without an audit row of their own (`agent_hub.events.store`). `summarise` (the job
`audit.event_batch`, every `INTERVAL_SECONDS`) finds the scopes whose head moved past `hub.event_audit_cover`, reads
their new events up to the head it saw, and appends for every (session, author) of them one row: actor and device the
author device, the organization, workgroup, task and session of the events, target the first event, detail
`{scope, count, first_scope_seq, last_scope_seq, ct_bytes}`. In the same transaction the cover of the scope moves to
that head. Per scope the events commit in `scope_seq` order (the storing transaction locks the scope head), so every
event at or below a committed head is visible: none is skipped, none is counted twice, and a pass that fails or a hub
that stops only delays the rows. Passes of several hub processes are serialised by `EVENT_BATCH_LOCK`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit

BATCHED_KINDS = frozenset({"session.output", "session.terminal", "session.tool", "session.hook", "session.file"})
INTERVAL_SECONDS = 60.0
EVENT_BATCH_LOCK = 742180104
"""Advisory lock of the job (next to the checkpoint lock 742180103)."""
MAX_SCOPES = 500
"""Scopes summarised per pass at most (the rest follow in the next pass)."""

_MOVED = (
    "SELECT h.scope_id, h.scope, h.seq, COALESCE(c.covered_seq, 0) FROM hub.scope_head h"
    " LEFT JOIN hub.event_audit_cover c ON c.scope_id = h.scope_id WHERE h.seq > COALESCE(c.covered_seq, 0)"
    " ORDER BY h.scope_id LIMIT %s"
)
_GROUPS = (
    "SELECT session_id, author_id, org_id, wg_id, task_id, count(*), min(scope_seq), max(scope_seq), sum(size),"
    " (array_agg(id ORDER BY scope_seq))[1] FROM hub.event"
    " WHERE scope_id = %s AND scope_seq > %s AND scope_seq <= %s AND kind = ANY(%s)"
    " GROUP BY session_id, author_id, org_id, wg_id, task_id ORDER BY min(scope_seq)"
)


def _row(scope: str, group: tuple[Any, ...], now: datetime) -> AuditRow:
    session, author, org, wg, task, count, first, last, size, first_id = group
    return AuditRow(
        at=now,
        actor_kind="device",
        actor_id=author,
        device_id=author,
        action="event.append_batch",
        result="ok",
        org_id=org,
        wg_id=wg,
        task_id=task,
        session_id=session,
        target_kind="event",
        target_id=str(first_id),
        event_id=UUID(str(first_id)),
        detail={
            "scope": scope,
            "count": int(count),
            "first_scope_seq": int(first),
            "last_scope_seq": int(last),
            "ct_bytes": int(size),
        },
    )


def summarise(connection: psycopg.Connection[Any], now: datetime, *, max_scopes: int = MAX_SCOPES) -> int:
    """One pass (module docstring); the number of rows appended."""
    appended = 0
    with connection.transaction():
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (EVENT_BATCH_LOCK,))
        rows: list[AuditRow] = []
        for scope_id, scope, head, covered in connection.execute(_MOVED, (max_scopes,)).fetchall():
            groups = connection.execute(_GROUPS, (scope_id, covered, head, sorted(BATCHED_KINDS))).fetchall()
            rows += [_row(str(scope), tuple(group), now) for group in groups]
            connection.execute(
                "INSERT INTO hub.event_audit_cover (scope_id, covered_seq) VALUES (%s, %s)"
                " ON CONFLICT (scope_id) DO UPDATE SET covered_seq = EXCLUDED.covered_seq",
                (scope_id, head),
            )
        # the global audit lock is taken by the first append only, after every query of the pass (review M2)
        for row in rows:
            append_audit(connection, row)
            appended += 1
    return appended


def run_summary(context: Any) -> int:
    """One pass of the job on the hub clock."""
    with context.pool.connection() as connection:
        return summarise(connection, context.clock())
