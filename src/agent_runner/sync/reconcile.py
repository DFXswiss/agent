"""Reconcile step `outbox` (order 45): scope queues whose scope the device may read no more are discarded with an audit
(KOR-08, ADR-047; 05-runner.md sections 2.2 and 3.5, 03-api.md section 6.1).

A scope queue (`sync.outbox`) holds events the device cannot seal yet. After `device_unassign` (and the rotation it
comes with) or `remove` of this device the assignment step (order 40) deletes the scope's keys, so events written
for that scope and not sealed before can never be sealed; the hub would refuse them anyway (`author_not_writer`, E5 at
receipt), as it refuses a poison item without stopping the stream. This step discards such a queue when the verified
log shows that the device may not read the scope (`OrgState.can_read`), and records it: one outbox item `audit`
`runner.alert` with `{alert: "outbox_discarded", value: <items>}` and target `org`/`workgroup` (metadata only, no
content) and one log line `runner.sync outbox_discarded scope=... scope_id=... items=...`.

Nothing is discarded without verified evidence: a queue of a stopped org (rollback, fork), of a scope that belongs to
no verified org, or of any scope while the platform log is stopped stays, as does a queue of a scope the device may
still read (its wrap is only missing so far); such a queue holds back its own scope and nothing else.
"""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.log.state import ORG
from agent_runner.orgs.book import OrgBook
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sync import outbox
from agent_runner.sync.runtime import SyncRuntime

ORDER = 45
ALERT = "outbox_discarded"
TARGET_KINDS = {"org": "org", "wg": "workgroup"}


def _org_of(book: OrgBook, scope: str, scope_id: str) -> str | None:
    if scope == ORG:
        return scope_id if scope_id in book.logs else None
    for org, log in book.logs.items():
        if scope_id in log.state.workgroups:
            return org
    return None


async def discardable(context: Any, book: OrgBook) -> list[tuple[str, str]]:
    """`(scope, scope_id)` of the scope queues the verified logs show this device may read no more."""
    if book.platform_stopped is not None:
        return []
    async with context.pool.connection() as conn:
        queues = await outbox.held_scopes(conn)
    found = []
    for scope, scope_id, _count in queues:
        org = _org_of(book, scope, scope_id)
        if org is None or org in book.stopped:
            continue
        if not book.logs[org].state.can_read(book.device_id, scope, scope_id):
            found.append((scope, scope_id))
    return found


async def discard(context: Any, sync: SyncRuntime, scope: str, scope_id: str) -> int:
    """Drop one scope queue and write its audit in the same transaction; returns the number of items dropped."""
    async with context.pool.connection() as conn, conn.transaction():
        count = await outbox.discard(conn, scope, scope_id)
        if count:
            alert = {
                "action": "runner.alert",
                "result": "failed",
                "target_kind": TARGET_KINDS[scope],
                "target_id": scope_id,
                "detail": {"alert": ALERT, "value": count},
            }
            await sync.enqueue(conn, "audit", alert)
    if count:
        context.log.write(f"runner.sync {ALERT} scope={scope} scope_id={scope_id} items={count}\n")
        context.log.flush()
    return count


async def step(run: Run) -> None:
    for scope, scope_id in await discardable(run.context, run.book):
        await discard(run.context, run.sync, scope, scope_id)


STEPS = [ReconcileStep(ORDER, "outbox", step)]
