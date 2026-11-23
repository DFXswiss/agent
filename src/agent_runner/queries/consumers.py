"""Inbox consumer of the read area (ADR-039): keeps the verified events the session reads answer from.

The inbox already checked E1-E5 (author signature and right at `lh`: `wg:write` for prompts and workgroup or task
whiteboards, org admin for org whiteboards and device profiles, the author device assigned for session outputs) and
decrypted the event with a held scope key. The consumer keeps the event as received with the author's signing key
from the verified log at `lh` (`projection.keep`); an event without the object it must name (a chat item without
task, a whiteboard or profile event without `ref`) is ignored, like the hub projection ignores it.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.inbox.dispatch import Consumer, VerifiedEvent
from agent_runner.orgs.book import book_for
from agent_runner.queries import projection


async def keep_read_event(context: Any, conn: AsyncConnection[Any], event: VerifiedEvent) -> None:
    if not projection.keepable(event.header):
        return
    log = book_for(context).log(event.org_id)
    lh = event.header["lh"]
    if log is None or not isinstance(lh, int) or lh > log.head.seq:
        return  # verified with the same book a moment ago; never block the scope for it
    await projection.keep(
        conn,
        header=event.header,
        scope_seq=event.scope_seq,
        received_at=event.received_at,
        author_sig=log.states[lh].principals[event.author].sig,
        sealed=event.sealed,
    )


CONSUMERS = [Consumer("queries.read", projection.KINDS, keep_read_event)]
