"""The final text and the turn end of a child session, read from the local outbox (05-runner.md section 13, ADR-038).

The session area turns every guest `session.output` and `session.hook` record into a device event whose draft the
outbox keeps, sealed with the storage key (`sync.outbox`, 05 section 2.3: local storage before the network). The
subagent area reads these drafts back instead of keeping a second copy of confidential text: newest first, only
`event` items written since the child was created (one minute of margin for the store clock), only drafts whose
header names the child. Drafts that wait in a scope queue (`runner.outbox_held`, KOR-08/ADR-047) are newer than every
numbered item and are read first; both are read in one snapshot.

- `final_text`: the last assistant message of the child = the newest `session.output` record and the records of the
  same message (the guest writes a block that arrives after its message as `<message_id>-<n>`, `transcript.py`),
  joined by a blank line in journal order and cut to 64 KiB. A child without output has the final text `""`.
- `turn_end`: `Stop` or `StopFailure` of the newest such `session.hook` record of the child (None without one).
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import timedelta
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions.records import LocalSession
from agent_runner.subagents.rules import cut_text
from agent_runner.sync.outbox import event_drafts

BATCH = 256
MARGIN = timedelta(minutes=1)
TURN_ENDS = frozenset({"Stop", "StopFailure"})
_PART_RE = re.compile(r"^(.+)-[0-9]+$")


async def drafts(
    conn: AsyncConnection[Any], storage_key: bytes, session: LocalSession
) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
    """`(kind, body)` of the child's event drafts, newest first (`outbox.event_drafts`, one snapshot)."""
    async with aclosing(event_drafts(conn, storage_key, session.created_at - MARGIN, BATCH)) as found:
        async for draft in found:
            header = draft["header"]
            if header["session"] == session.id:
                yield str(header["kind"]), draft["body"]


def _same_message(newest: str, message_id: str) -> bool:
    part = _PART_RE.fullmatch(newest)
    base = newest if part is None else part.group(1)
    return message_id in (newest, base) or (message_id.startswith(base + "-") and message_id[len(base) + 1 :].isdigit())


async def final_text(conn: AsyncConnection[Any], storage_key: bytes, session: LocalSession) -> str:
    parts: list[str] = []
    newest: str | None = None
    async with aclosing(drafts(conn, storage_key, session)) as items:
        async for kind, body in items:
            if kind != "session.output":
                continue
            message_id = str(body.get("message_id", ""))
            if newest is None:
                newest = message_id
            elif not _same_message(newest, message_id):
                break
            parts.append(str(body.get("text", "")))
    return cut_text("\n\n".join(reversed(parts)))


async def turn_end(conn: AsyncConnection[Any], storage_key: bytes, session: LocalSession) -> str | None:
    async with aclosing(drafts(conn, storage_key, session)) as items:
        async for kind, body in items:
            event = body.get("event")
            if kind == "session.hook" and event in TURN_ENDS:
                return str(event)
    return None
