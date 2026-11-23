"""Device events and the failure of a session (03-api.md sections 6.1 and 10, 04-crypto.md section 4.3).

- `session_event(conn, outbox, session, kind=..., body=..., ts=..., ref="")`: the outbox draft of a device-authored
  session event (wg scope of the session's workgroup, its task and session in the header, `ref` empty unless the kind
  needs one - `platform.read` names its read target, 04-crypto.md section 4.3 -, a fresh UUIDv7); it is sealed at the
  first push with the current workgroup key and the device key (`sync.seal`).
- `fail(conn, outbox, session_id, reason, at=...)`: the `failed` transition with its reason and, when it was a
  transition, the `session.status` event `{state: "failed", reason}` so the chat shows how the session ended. Every
  failure path (start, journal, interrupted start, unreachable VM) uses it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.crypto.ids import uuid7
from agent_runner.sessions import records
from agent_runner.sessions.records import LocalSession, Outbox
from agent_runner.sync.outbox import event_draft


async def session_event(
    conn: AsyncConnection[Any],
    outbox: Outbox,
    session: LocalSession,
    *,
    kind: str,
    body: Mapping[str, Any],
    ts: str,
    ref: str = "",
) -> None:
    draft = event_draft(
        id=uuid7(),
        org=session.org_id,
        scope="wg",
        scope_id=session.wg_id,
        wg=session.wg_id,
        task=session.task_id,
        session=session.id,
        kind=kind,
        ts=ts,
        body=body,
        ref=ref,
    )
    await outbox.enqueue(conn, "event", draft)


async def fail(
    conn: AsyncConnection[Any],
    outbox: Outbox,
    session_id: str,
    reason: str,
    *,
    at: str,
    fields: Mapping[str, object] | None = None,
) -> LocalSession | None:
    """`failed` with `reason` plus its `session.status` event; None when the session had already ended."""
    moved = await records.advance(conn, outbox, session_id, "failed", at=at, reason=reason, fields=fields)
    if moved is not None:
        status = {"state": "failed", "reason": reason}
        await session_event(conn, outbox, moved, kind="session.status", body=status, ts=at)
    return moved
