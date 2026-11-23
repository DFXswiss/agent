"""Inbox consumer of this area (ADR-039): keeps the latest profile events for the harness context (`profiles`).

The event already passed every check of the inbox (E1-E5 at `lh`, decryption); the author's signing key is taken from
the verified log at `lh`, so the stored event can be opened again later with the same check.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.inbox.dispatch import Consumer, VerifiedEvent
from agent_runner.orgs.book import book_for
from agent_runner.sessions import profiles


async def keep_profile(context: Any, conn: AsyncConnection[Any], event: VerifiedEvent) -> None:
    log = book_for(context).log(event.org_id)
    lh = event.header["lh"]
    if log is None or not isinstance(lh, int) or lh > log.head.seq:
        return
    author = log.states[lh].principals.get(event.author)
    if author is None:
        return
    await profiles.remember(conn, header=event.header, sealed=event.sealed, author_sig=author.sig)


CONSUMERS = [Consumer("sessions.profiles", profiles.KINDS, keep_profile)]
