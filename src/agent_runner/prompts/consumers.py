"""Inbox consumer of this area (ADR-039): keeps every verified `prompt` event for its delivery (`queue.keep`).

The inbox already checked E1-E5 with the author's right `wg:write` at `lh` and decrypted the event with a held scope
key; the consumer additionally requires that the prompt addresses a task (header `session` and `ref` empty, 03-api.md
section 10) - otherwise it is refused and audited like every other failed inbound check - and keeps the event as
received with the author's signing key from the verified log at `lh`, so it is opened again only into memory when it
is delivered. A new prompt wakes the delivery.
"""

from __future__ import annotations

from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.inbox.dispatch import Consumer, VerifiedEvent
from agent_runner.orgs import audit
from agent_runner.orgs.book import book_for
from agent_runner.prompts import deliver, queue
from agent_runner.prompts.verify import MISROUTED, addresses_task
from agent_runner.sync.runtime import runtime


async def keep_prompt(context: Any, conn: AsyncConnection[Any], event: VerifiedEvent) -> None:
    if not addresses_task(event.header):
        await audit.reject(
            context,
            cast(Any, runtime(context)),
            conn,
            source="event",
            code=MISROUTED,
            target_kind="event",
            target_id=event.event_id,
        )
        return
    log = book_for(context).log(event.org_id)
    lh = event.header["lh"]
    assert log is not None
    assert isinstance(lh, int)
    author = log.states[lh].principals[event.author]
    stored = await queue.keep(
        conn, header=event.header, scope_seq=event.scope_seq, author_sig=author.sig, sealed=event.sealed
    )
    found = deliver.existing(context)
    if stored and found is not None:
        found.wake()


CONSUMERS = [Consumer("prompts.events", frozenset({"prompt"}), keep_prompt)]
