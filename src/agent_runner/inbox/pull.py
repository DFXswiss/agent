"""Inbox: pull loop, cursors and inbound event verification (05-runner.md sections 2.3 and 2.4, 03-api.md section 6.1
`GET /v1/device/pull`, 04-crypto.md sections 6.1 (L10) and 6.4, 02-data-model.md section 4 `inbox_cursor`).

The device pulls the scopes it is entitled to by its **verified** logs (`OrgBook.pull_scopes`: the workgroups it is
assigned to in acting orgs and their org scopes), one cursor per scope (`runner.inbox_cursor`, the highest
`scope_seq` handled). `pull_all` repeats `GET /v1/device/pull?cursor=<scope>:<seq>&...` until the hub answers
`more:false` or nothing advances. Triggers: the `events` hint, the `pull` connect hook and every 30 s (task `inbox`).

Every event is checked before anything acts on it, in this order:

1. the item belongs to a pulled scope and is new (`scope_seq` above the cursor; gaps are normal because the hub
   never sends `session.terminal` to devices); the header names the same scope and org and is well-formed (E1/E2);
   an event ID handled before by this daemon under another `scope_seq` is a replay (`event_conflict`);
2. L10: an `lh` beyond the verified head refreshes the platform log and that org's log; if the hub's own head reaches
   `lh` but its entries did not verify, the event is refused (`log_chain`); if the hub withholds them it is a
   `log_fork` (the org is stopped, alerted and audited like in reconcile step 2);
3. E3-E5 against the verified log at `lh` (`crypto.log.events.check_event`: kind for scope and author kind, author
   signature, the kind's right at `lh` - `wg:write` for prompts and settings), decertified platform devices excluded;
   an author outside the org log is checked against the support persons of the verified platform log (ADR-096; a
   refusal `author_not_writer` of such an author refreshes the platform log once and checks again);
4. only for kinds a consumer is registered for: the scope key of the header's epoch (one wrap refresh per pull when
   it is missing). Still missing: if the log says the device held that epoch, the scope waits for the wrap (held,
   retried at the next pull); otherwise (an epoch before the device's assignment) the event is skipped with a log
   line. Then decryption and a body JSON object.

A refused event is ignored, audited (`runner.inbound_rejected`, source `event`), counted, and its cursor advances.
A verified event is handed to the consumers of its kind (`inbox.dispatch`) in the transaction that advances the
cursor. Events of kinds without a consumer only advance the cursor. A scope that waits (failing consumer, missing
key, stopped org) is left out of the following rounds of the same pull, so it never starves the other scopes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import check_header, open_event
from agent_runner.crypto.log.events import check_event
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.inbox.dispatch import Consumer, VerifiedEvent, consumers
from agent_runner.orgs import audit
from agent_runner.orgs.book import OrgBook, book_for
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import load_scope_key
from agent_runner.reconcile import logs, wire, wraps
from agent_runner.reconcile.engine import Run, describe
from agent_runner.sync.runtime import runtime

TARGET = "/v1/device/pull"
PERIOD_S = 30.0
PAGE_LIMIT = 200
MAX_ROUNDS = 1000
MAX_SEEN = 100_000
ATTRIBUTE = "inbox"


@dataclass(frozen=True)
class _Item:
    scope_id: str
    scope_seq: int
    received_at: str
    event: object


@dataclass(frozen=True)
class _Rejected:
    code: str


class _Stopped(Exception):
    """The event's org was stopped while verifying it (L10 fork)."""


class _Held(Exception):
    """The device is entitled to the event's epoch key but does not hold it yet: the scope waits."""


def parse_page(answer: Mapping[str, Any]) -> tuple[list[_Item], bool]:
    """`{events:[{scope_id, scope_seq, received_at, event}], more}`, sorted by scope and `scope_seq`."""
    items: list[_Item] = []
    for raw in wire.array(answer.get("events"), "events"):
        data = wire.obj(raw, "events")
        if set(data) != {"scope_id", "scope_seq", "received_at", "event"} or not isinstance(data["received_at"], str):
            raise wire.ResponseInvalid("events")
        items.append(
            _Item(
                wire.uuid_text(data["scope_id"], "events"),
                wire.seq(data["scope_seq"], "events", 1),
                data["received_at"],
                data["event"],
            )
        )
    more = answer.get("more")
    if not isinstance(more, bool):
        raise wire.ResponseInvalid("more")
    return sorted(items, key=lambda item: (item.scope_id, item.scope_seq)), more


async def load_cursors(conn: AsyncConnection[Any], scope_ids: Sequence[str]) -> dict[str, int]:
    """The cursor of each scope (0 for a scope never pulled)."""
    cur = await conn.execute(
        "SELECT scope_id::text, scope_seq FROM runner.inbox_cursor WHERE scope_id = ANY(%s::uuid[])", (list(scope_ids),)
    )
    found = {str(scope_id): int(seq) for scope_id, seq in await cur.fetchall()}
    return {scope_id: found.get(scope_id, 0) for scope_id in scope_ids}


async def save_cursor(conn: AsyncConnection[Any], scope_id: str, scope_seq: int) -> None:
    await conn.execute(
        "INSERT INTO runner.inbox_cursor (scope_id, scope_seq) VALUES (%s, %s) ON CONFLICT (scope_id) "
        "DO UPDATE SET scope_seq = GREATEST(runner.inbox_cursor.scope_seq, EXCLUDED.scope_seq)",
        (scope_id, scope_seq),
    )


class Inbox:
    def __init__(self, context: Any, registered: Sequence[Consumer]) -> None:
        self.context = context
        self.consumers = list(registered)
        self.trigger = asyncio.Event()
        self.lock = asyncio.Lock()
        self.pulls = 0
        self.seen: set[str] = set()

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.inbox {text}\n")
        self.context.log.flush()

    def request(self) -> None:
        self.trigger.set()

    def consumers_of(self, kind: str) -> list[Consumer]:
        return [consumer for consumer in self.consumers if kind in consumer.kinds]

    # --- one pull ----------------------------------------------------------------------------------------------

    async def pull_all(self) -> int:
        """Pull every scope until the hub has nothing more; returns the number of events handled."""
        sync = runtime(self.context)
        if sync is None:
            return 0
        book = book_for(self.context)
        async with self.lock, session(self.context.pool) as conn:
            async with book.lock:
                await book.ensure_loaded(conn)
            run = Run(context=self.context, sync=sync, book=book)
            handled = 0
            keys_refreshed = [False]
            blocked: set[str] = set()
            for _ in range(MAX_ROUNDS):
                # a blocked scope (failing consumer, missing key, stopped org) is left out of the next rounds, so it
                # never fills the pages of the other scopes
                scopes = [scope_id for scope_id in book.pull_scopes() if scope_id not in blocked]
                if not scopes:
                    break
                cursors = await load_cursors(conn, sorted(scopes))
                query = "&".join(f"cursor={scope_id}:{cursors[scope_id]}" for scope_id in sorted(scopes))
                items, more = parse_page(await sync.client.request("GET", f"{TARGET}?{query}&limit={PAGE_LIMIT}"))
                advanced = await self._page(
                    run, conn, items, cursors=cursors, refreshed=keys_refreshed, blocked=blocked
                )
                handled += advanced
                if not more or advanced == 0:
                    break
            self.pulls += 1
            return handled

    async def _page(
        self,
        run: Run,
        conn: AsyncConnection[Any],
        items: list[_Item],
        *,
        cursors: dict[str, int],
        refreshed: list[bool],
        blocked: set[str],
    ) -> int:
        advanced = 0
        for item in items:
            scopes = run.book.pull_scopes()
            if item.scope_id not in scopes or item.scope_id not in cursors:
                self.line(f"unexpected_scope scope={item.scope_id}")
                continue
            if item.scope_id in blocked or item.scope_seq <= cursors[item.scope_id]:
                continue
            org, scope = scopes[item.scope_id]
            try:
                result = await self._verify(run, conn, item, org=org, scope=scope, refreshed=refreshed)
            except (_Stopped, _Held):
                blocked.add(item.scope_id)
                continue
            try:
                await self._commit(run, conn, org, item, result)
            except Exception as exc:
                self.line(f"consumer_failed scope={item.scope_id} seq={item.scope_seq} error={describe(exc)}")
                blocked.add(item.scope_id)
                continue
            cursors[item.scope_id] = item.scope_seq
            advanced += 1
        return advanced

    async def _commit(
        self, run: Run, conn: AsyncConnection[Any], org: str, item: _Item, result: VerifiedEvent | _Rejected | None
    ) -> None:
        async with conn.transaction():
            if isinstance(result, _Rejected):
                await audit.reject(
                    self.context,
                    run.sync,
                    conn,
                    source="event",
                    code=result.code,
                    target_kind="event",
                    target_id=f"{item.scope_id}:{item.scope_seq}",
                )
            elif result is not None:
                for consumer in self.consumers_of(result.kind):
                    await consumer.handle(self.context, conn, result)
            await save_cursor(conn, item.scope_id, item.scope_seq)
        if not isinstance(result, _Rejected):
            self.remember(cast(dict[str, Any], item.event)["header"]["id"])

    def remember(self, event_id: str) -> None:
        """Keep the ID of a handled event (bounded): the same signed event under another `scope_seq` is a replay."""
        if len(self.seen) >= MAX_SEEN:
            self.seen.clear()
        self.seen.add(event_id)

    async def _refresh_log(self, run: Run, conn: AsyncConnection[Any], org: str, lh: int) -> _Rejected | None:
        """L10: refresh the platform log (a new entry may name a newer certification) and the org's log for an `lh`
        beyond the verified head. Still beyond it: when the hub's own head reaches `lh`, its entries up to there were
        refused (the event cannot be verified: `log_chain`); otherwise the hub withholds what the author saw, a
        `log_fork` that stops the org."""
        book = run.book
        result: _Rejected | None = None
        async with book.lock:
            await logs.refresh_platform(run, conn)
            answered = await logs.refresh_org(run, conn, org, None)
            log = book.log(org)
            if log is not None and lh > log.head.seq and answered is not None and answered.seq >= lh:
                result = _Rejected("log_chain")
            elif log is not None and lh > log.head.seq:
                async with conn.transaction():
                    await book.stop(conn, org, "log_fork")
                    await audit.log_alert(
                        self.context,
                        run.sync,
                        conn,
                        code="log_fork",
                        target_kind="org",
                        target_id=org,
                        verified_seq=log.head.seq,
                        offered_seq=lh,
                    )
        if book.log(org) is None:
            raise _Stopped
        return result

    async def _check(
        self, run: Run, conn: AsyncConnection[Any], item: _Item, *, org: str, scope: str
    ) -> tuple[dict[str, object], VerifiedLog] | _Rejected:
        """Steps 1-3 of the module docstring: the checked header and the log, or a rejection."""
        book: OrgBook = run.book
        event = item.event
        header = event.get("header") if isinstance(event, dict) else None
        if not isinstance(header, dict) or (header.get("org"), header.get("scope"), header.get("scope_id")) != (
            org,
            scope,
            item.scope_id,
        ):
            return _Rejected("header_invalid")
        try:
            # E1/E2 before anything else: a malformed header never triggers a refresh or a stop
            checked_header = check_header(header)
        except CryptoError as refused:
            return _Rejected(refused.code)
        if checked_header["id"] in self.seen:
            return _Rejected("event_conflict")
        lh = cast(int, checked_header["lh"])
        head = book.head(org)
        if head is not None and lh > head.seq:
            refused_lh = await self._refresh_log(run, conn, org, lh)
            if refused_lh is not None:
                return refused_lh
        log = book.log(org)
        # the org is acting (its scope was pulled) and a refresh that stopped it raised `_Stopped` above
        assert log is not None
        return await self._check_event(run, conn, event, log, org=org, author=str(checked_header["author"]))

    async def _check_event(
        self, run: Run, conn: AsyncConnection[Any], event: object, log: VerifiedLog, *, org: str, author: str
    ) -> tuple[dict[str, object], VerifiedLog] | _Rejected:
        """Step 3; an author outside the org log may be a support person this copy of the platform log does not know
        yet (ADR-096): a refusal `author_not_writer` refreshes the platform log once and checks again."""
        book: OrgBook = run.book
        try:
            checked, _rule = check_event(event, log, excluded=book.excluded(org), support=book.support_roster())
        except CryptoError as refused:
            if refused.code != "author_not_writer" or author in log.state.principals:
                return _Rejected(refused.code)
        else:
            return checked, log
        async with book.lock:
            await logs.refresh_platform(run, conn)
        try:
            checked, _rule = check_event(event, log, excluded=book.excluded(org), support=book.support_roster())
        except CryptoError as again:
            return _Rejected(again.code)
        return checked, log

    async def _verify(
        self, run: Run, conn: AsyncConnection[Any], item: _Item, *, org: str, scope: str, refreshed: list[bool]
    ) -> VerifiedEvent | _Rejected | None:
        """The verified event, a rejection, or None (verified, but no consumer or no key: only the cursor moves)."""
        checked = await self._check(run, conn, item, org=org, scope=scope)
        if isinstance(checked, _Rejected):
            return checked
        header, log = checked
        if not self.consumers_of(str(header["kind"])):
            return None
        epoch = cast(int, header["epoch"])
        key = await load_scope_key(conn, run.sync.storage_key, scope, item.scope_id, epoch)
        if key is None and not refreshed[0]:
            refreshed[0] = True
            await wraps.sync_wraps(run)
            key = await load_scope_key(conn, run.sync.storage_key, scope, item.scope_id, epoch)
        if key is None and run.book.device_id in log.epoch_holders(scope, item.scope_id, epoch):
            self.line(f"key_waiting scope={item.scope_id} seq={item.scope_seq} epoch={epoch}")
            raise _Held
        if key is None:
            self.line(f"key_missing scope={item.scope_id} seq={item.scope_seq} epoch={epoch}")
            return None
        author_sig = run.book.author_sig(org, cast(int, header["lh"]), str(header["author"]))
        try:
            body = parse_json(open_event(key, item.event, b64u_decode(author_sig)))
        except CryptoError as refused:
            return _Rejected(refused.code)
        if not isinstance(body, dict):
            return _Rejected("header_invalid")
        return VerifiedEvent(
            org_id=org,
            scope=scope,
            scope_id=item.scope_id,
            scope_seq=item.scope_seq,
            received_at=item.received_at,
            header=header,
            body=body,
            sealed=cast(dict[str, object], item.event),
        )

    # --- loop --------------------------------------------------------------------------------------------------

    async def wait(self, seconds: float, stopping: asyncio.Event) -> None:
        waiters = {
            asyncio.ensure_future(self.trigger.wait()),
            asyncio.ensure_future(stopping.wait()),
            asyncio.ensure_future(self.context.host.sleep(seconds)),
        }
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def loop(self, stopping: asyncio.Event) -> None:
        """Pull every `PERIOD_S` and on request until `stopping`; a failed pull is logged and retried."""
        while not stopping.is_set():
            self.trigger.clear()
            try:
                await self.pull_all()
            except Exception as exc:
                self.line(f"pull_failed error={describe(exc)}")
            await self.wait(PERIOD_S, stopping)


def inbox_for(context: Any) -> Inbox | None:
    """The inbox of this daemon with the discovered consumers; None while the device is not enrolled."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Inbox):
        return found
    if context.identity is None:
        return None
    inbox = Inbox(context, consumers(context.host.package))
    setattr(context, ATTRIBUTE, inbox)
    return inbox
