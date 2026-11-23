"""Reconcile step 2: fetch and verify the platform log and the membership logs (05-runner.md section 2.2,
04-crypto.md sections 6.1 and 6.2, 03-api.md section 6.1 `GET /v1/device/platform-log`, `GET /v1/device/logs`).

1. Platform log: the entries after the verified head, verified P1-P4 against the configured root key and stored
   (`OrgBook.extend_platform`). The run halts unless the verified log certifies this device with its own keys
   (`uncertified`, `decertified`): nothing is acted upon before that. An org device (RUN-06) is never certified; it
   keeps the platform log only to verify the platform devices assigned in its org.
2. For every org of the desired state, compared with the offered head (`orgs[].log_head`):
   - offered `seq` below the verified head: `log_rollback` (L9) - unless it is below only entries verified after
     step 1 requested the state (`Run.asked_heads`: the inbox's L10 refresh may run in between) and is a head of the
     verified history: then the offer is just older than the copy and the hub's current log decides, as in the L10
     refresh (an answer below the verified head is still a rollback);
   - same `seq`, other hash: `log_fork`;
   - otherwise the entries after the verified head (from `seq` 0 for a new org) are fetched page by page, verified
     (L1-L8) and stored. A refused entry that continues the verified head with another predecessor is a `log_fork`;
     an answer whose head lies below the verified head is a `log_rollback`.
   A rollback or fork stops acting for **that org only** (persisted), raises `runner.alert` and writes the audit
   items `runner.log_rollback` / `runner.log_fork`; the other orgs go on. Any other refused entry is ignored and
   audited (`runner.inbound_rejected`, source `log`); the verified entries before it are kept.
The same rules apply to the platform log, whose rollback or fork stops every org (target `platform_log`).

`refresh_org` is also the L10 refresh of the inbox (an event names a head beyond the verified one). `refresh_acting`
refreshes the platform log and every acting org's log before an inbound item whose refusal depends only on the log
state is judged (a key wrap, decisions/HOT-04.md; a device request, decisions/KOR-17.md).
"""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.log.entries import Head
from agent_runner.orgs import audit
from agent_runner.orgs.book import Outcome
from agent_runner.orgs.db import session
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Run
from agent_runner.sync.http import HubRefusal, HubTransportError

PLATFORM_TARGET = "/v1/device/platform-log"
LOGS_TARGET = "/v1/device/logs"
PAGE_LIMIT = 1000
MAX_PAGES = 1000
PLATFORM_KIND = "platform_log"
PLATFORM_ID = "platform"


async def fetch_pages(run: Run, target: str, query: str, after: int | None) -> tuple[list[object], Head]:
    """Every entry after `after` (from the start for None), following `next_after` while it advances."""
    entries: list[object] = []
    page_head: Head | None = None
    for _ in range(MAX_PAGES):
        position = "" if after is None else f"&after={after}"
        answer = await run.sync.client.request("GET", f"{target}?{query}limit={PAGE_LIMIT}{position}")
        page, page_head, next_after = wire.log_page(answer)
        entries.extend(page)
        if next_after is None or not page or (after is not None and next_after <= after):
            break
        after = next_after
    assert page_head is not None
    return entries, page_head


async def _alert(run: Run, conn: Any, *, org: str | None, code: str, verified: Head, offered: int) -> None:
    async with conn.transaction():
        await run.book.stop(conn, org, code)
        await audit.log_alert(
            run.context,
            run.sync,
            conn,
            code=code,
            target_kind=PLATFORM_KIND if org is None else "org",
            target_id=PLATFORM_ID if org is None else org,
            verified_seq=verified.seq,
            offered_seq=offered,
        )


def _divergence(local: Head | None, offered: Head | None) -> str | None:
    """`log_rollback` / `log_fork` when the offered head cannot extend the verified one."""
    if local is None or offered is None or offered.seq > local.seq:
        return None
    if offered.seq < local.seq:
        return "log_rollback"
    return None if offered.hash == local.hash else "log_fork"


async def _settle(run: Run, conn: Any, *, org: str | None, local: Head | None, offered: Head, outcome: Outcome) -> None:
    """Alert a fork found while verifying, audit any other refused entry (once per head, code and org)."""
    if outcome.alert is not None and local is not None:
        await _alert(run, conn, org=org, code=outcome.alert, verified=local, offered=offered.seq)
        return
    if outcome.error is None:
        return
    head = run.book.head(org) if org is not None else run.book.platform_head
    key = ("log", org, 0 if head is None else head.seq + 1, outcome.error)
    if not run.book.remember_refusal(key):
        return
    async with conn.transaction():
        await audit.reject(
            run.context,
            run.sync,
            conn,
            source="log" if org is not None else PLATFORM_KIND,
            code=outcome.error,
            target_kind="org" if org is not None else PLATFORM_KIND,
            target_id=org if org is not None else PLATFORM_ID,
        )


async def refresh_platform(run: Run, conn: Any) -> None:
    book = run.book
    if book.platform_stopped is not None:
        return
    local = book.platform_head
    entries, offered = await fetch_pages(run, PLATFORM_TARGET, "", None if local is None else local.seq)
    divergence = None if entries else _divergence(local, offered)
    if divergence is not None:
        assert local is not None
        await _alert(run, conn, org=None, code=divergence, verified=local, offered=offered.seq)
        return
    outcome = await book.extend_platform(conn, entries)
    await _settle(run, conn, org=None, local=local, offered=offered, outcome=outcome)


async def refresh_org(run: Run, conn: Any, org: str, offered: Head | None) -> Head | None:
    """Bring the copy of one org up to the offered head (None: whatever the hub has, the inbox's L10 refresh).

    Returns the head the hub answered (None when nothing was fetched)."""
    book = run.book
    if org in book.stopped:
        return None
    local = book.head(org)
    divergence = _divergence(local, offered)
    entries: list[object] = []
    answered: Head | None = None
    if divergence is None:
        if local is not None and offered == local:
            return None
        entries, answered = await fetch_pages(run, LOGS_TARGET, f"org={org}&", None if local is None else local.seq)
        divergence = None if entries else _divergence(local, answered)
        offered = answered
    if divergence is not None:
        assert local is not None
        assert offered is not None
        await _alert(run, conn, org=org, code=divergence, verified=local, offered=offered.seq)
        return answered
    assert offered is not None
    outcome = await book.extend_org(conn, org, entries)
    await _settle(run, conn, org=org, local=local, offered=offered, outcome=outcome)
    return answered


async def refresh_acting(run: Run, conn: Any) -> bool:
    """Bring the platform log and every acting org's log up to the hub's current heads (as the L10 refresh), each on
    its own under the book lock; False when any of them could not be asked or the hub answered something unusable."""
    book = run.book
    complete = True
    async with book.lock:
        for org in (None, *book.acting_orgs()):
            try:
                if org is None:
                    await refresh_platform(run, conn)
                else:
                    await refresh_org(run, conn, org, None)
            except (HubTransportError, HubRefusal, wire.ResponseInvalid):
                complete = False
    return complete


def current_offer(run: Run, org: str, offered: Head) -> Head | None:
    """The offered head, or None when it is only older than the copy (see the module docstring, step 2)."""
    log = run.book.logs.get(org)
    asked = None if run.asked_heads is None else run.asked_heads.get(org)
    if log is None or asked is None or offered.seq >= log.head.seq or offered.seq < asked.seq:
        return offered
    return None if log.states[offered.seq].head == offered.hash else offered


async def sync_logs(run: Run) -> None:
    book = run.book
    assert run.desired is not None
    async with book.lock, session(run.pool) as conn:
        await book.ensure_loaded(conn)
        await refresh_platform(run, conn)
        if not book.org_device and not book.certified:
            run.halt(book.status())
            return
        for org, offered in wire.desired_orgs(run.desired):
            await refresh_org(run, conn, org, current_offer(run, org, offered))
