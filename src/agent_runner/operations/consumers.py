"""Inbox consumers of the operation area (ADR-039): verified `settings.workgroup` and `operation.result` events.

The inbox checked E1-E5 (the author's right at `lh`: `wg:write` for settings, the author device assigned to the
workgroup for results) and decrypted the event. The consumers check the body shape (`settings.parse`,
`results.valid`) - a failure is refused and audited like every failed inbound check (`runner.inbound_rejected`,
source `setting` / `event`) - and keep the event as received with the author's signing key from the verified log at
`lh`. A duplicate that waits for its original finds a stored result at its next poll.
"""

from __future__ import annotations

from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.inbox.dispatch import Consumer, VerifiedEvent
from agent_runner.operations import results, settings
from agent_runner.orgs import audit
from agent_runner.orgs.book import book_for
from agent_runner.sync.runtime import runtime


def _author_sig(context: Any, event: VerifiedEvent) -> str:
    log = book_for(context).log(event.org_id)
    lh = event.header["lh"]
    assert log is not None
    assert isinstance(lh, int)
    return log.states[lh].principals[event.author].sig


async def _reject(context: Any, conn: AsyncConnection[Any], event: VerifiedEvent, source: str, code: str) -> None:
    await audit.reject(
        context,
        cast(Any, runtime(context)),
        conn,
        source=source,
        code=code,
        target_kind="event",
        target_id=event.event_id,
    )


async def keep_settings(context: Any, conn: AsyncConnection[Any], event: VerifiedEvent) -> None:
    try:
        settings.parse(event.body)
    except settings.SettingsInvalid:
        await _reject(context, conn, event, "setting", settings.INVALID)
        return
    await settings.keep(
        conn,
        org_id=event.org_id,
        wg_id=event.scope_id,
        event_id=event.event_id,
        scope_seq=event.scope_seq,
        author_sig=_author_sig(context, event),
        sealed=event.sealed,
    )


async def keep_result(context: Any, conn: AsyncConnection[Any], event: VerifiedEvent) -> None:
    if not results.valid(event.header, event.body):
        await _reject(context, conn, event, "event", results.INVALID)
        return
    await results.keep(
        conn,
        operation_id=str(event.header["ref"]),
        event_id=event.event_id,
        org_id=event.org_id,
        wg_id=event.scope_id,
        author_sig=_author_sig(context, event),
        sealed=event.sealed,
    )


CONSUMERS = [
    Consumer("operations.settings", frozenset({"settings.workgroup"}), keep_settings),
    Consumer("operations.results", frozenset({"operation.result"}), keep_result),
]
