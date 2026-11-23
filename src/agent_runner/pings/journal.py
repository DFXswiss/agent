"""Journal handler `ping.delivered` (`<area>/journal.py:HANDLERS`, ADR-027 section 4; 03-api.md sections 6.1 and 7.2;
05-runner.md section 9).

The guest journals `ping.delivered {ping_id, via:"hook"|"tool"}` when a hook answer carried the notice or
`get_notifications` returned the ping. The handler runs in the journal page's transaction: for a session that runs on
this VM and a ping this device sent to that session, the first delivery sets `delivered_at` and enqueues the outbox
audit `ping.delivered {ping_id, kind, via}` (05-runner.md section 9). For a hub ping (`whiteboard.entry`) the audit also
marks the hub's `session_ping` row delivered; the runner's own pings (`subagent.finished`, `operation.finished`) have no
hub row and the hub audits them without one (KOR-07; before, RUN-14 recorded them locally only). A later delivery of
the same ping (the tool after the hook) changes nothing. An unknown ping is logged; a body that is not
`{ping_id, via}` is refused (`JournalRecordInvalid`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.guestlink.client import JournalRecord
from agent_runner.sessions.journal import JournalRecordInvalid

KIND = "ping.delivered"
VIA = frozenset({"hook", "tool"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def parse(body: Mapping[str, Any]) -> tuple[str, str]:
    ping_id, via = body.get("ping_id"), body.get("via")
    if set(body) != {"ping_id", "via"} or not isinstance(ping_id, str) or not _UUID_RE.fullmatch(ping_id):
        raise JournalRecordInvalid(KIND)
    if via not in VIA:
        raise JournalRecordInvalid(KIND)
    return ping_id, str(via)


async def ping_delivered(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    ping_id, via = parse(record.body)
    row = await (
        await conn.execute(
            "UPDATE runner.ping SET delivered_at = now() WHERE id = %s AND session_id = %s AND delivered_at IS NULL"
            " RETURNING kind",
            (ping_id, session.id),
        )
    ).fetchone()
    if row is None:
        known = await (
            await conn.execute("SELECT 1 FROM runner.ping WHERE id = %s AND session_id = %s", (ping_id, session.id))
        ).fetchone()
        if known is None:
            journal.env.log(f"runner.pings delivered_unknown ping_id={ping_id} session_id={session.id}")
        return
    kind = row[0]
    journal.env.log(f"runner.pings delivered ping_id={ping_id} session_id={session.id} kind={kind} via={via}")
    await journal.env.outbox.enqueue(
        conn,
        "audit",
        {
            "action": KIND,
            "result": "ok",
            "task_id": session.task_id,
            "session_id": session.id,
            "target_kind": "session",
            "target_id": session.id,
            "detail": {"ping_id": ping_id, "kind": kind, "via": via},
        },
    )


HANDLERS: dict[str, Callable[..., Any]] = {KIND: ping_delivered}
