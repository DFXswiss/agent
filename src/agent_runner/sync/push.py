"""Outbox push to the hub (03-api.md section 6.1, 05-runner.md section 2.3, 04-crypto.md section 5.12).

`Pusher.push_once()` sends one batch: the items after `pushed_origin_seq`, at most 500 items and 4 MiB of request
body. Pacing (KOR-13, ADR-048): a push starts at once when an item is due and no push started within the last
`MIN_INTERVAL_S` (leading edge); otherwise it starts `MIN_INTERVAL_S` after the *start* of the previous push (start to
start), so items written while a push is on the way are collected and leave together with the next one. One push is on
the way at a time (`lock`). The hub's pacer counts arrivals with a token bucket of the same rate and a reserve of
several pushes, so the jitter of the way never costs a `429`. A batch that had to wait is taken again after the wait,
so items committed meanwhile go with it (PERF-03). Before anything is sent the events of the batch are sealed (first
attempt) and the sealed bytes are committed, so every retry sends the same bytes.

Hedged push (ACC-FINAL, ADR-050): a push still unanswered `hedge_after` seconds after it left is sent once more, with
the same body and a new signature, on another connection of the pool (the first one is busy). The first verified
answer of the two counts and the other request is cancelled; when one of them fails, the other one's answer is
awaited, and when both fail the error of the first request propagates. The hub stores each `origin_seq` once; the
request that did not count may still reach it and is answered `replayed`, or refused without effect when an item of
its batch was stored meanwhile with other bytes (resealed, renumbered). Both requests have ended before `_post`
returns, so no later push reads that answer (ADR-050 sections 2 and 3). `hedge_after` is three times the median
time to an answer of the last 16 answered pushes, at least `HEDGE_MIN_S`: a single push that hangs on a lossy link (a
dropped segment waits for the retransmission timer) no longer holds every later item for half a second, while a hub
that is slow for every push (many busy sessions) is not sent each push twice. At most one copy is sent per
`HEDGE_SPACING_S` (the hub's pacer, ADR-048) and none for a batch above `HEDGE_MAX_BYTES`. `hedge_min=None` turns
hedging off.

Answer handling:

- `acked_through` advances `pushed_origin_seq` (the only way it moves forward); `rejected` items are logged and
  skipped (the hub acknowledged them); `reseal` marks the item to be sealed again with a newer epoch.
- `409 origin_seq_gap` rewinds to `detail.expected` and resends from there. When the outbox no longer holds
  `expected` (a hub restored from an export older than the retention of acknowledged items, KOR-10) and the hub's
  `GET /v1/device/state` confirms the position, the stored items from there on are renumbered to follow it and the
  pruned ones are reported lost (`outbox.realign`); pushing goes on. Unconfirmed, the push is retried.
- `409 origin_seq_conflict` after a power cut that cost the store its newest commits (HOT-25: the hub has items the
  store lost, and the store numbered new items the same) is repaired once per process: when no push of this process
  was answered yet, `GET /v1/device/state` confirms that the hub is at or past the first item of the batch, the first
  item after `pushed_origin_seq` is `pushed_origin_seq + 1` and the hub is at most `LOSS_LIMIT` items ahead, the
  runner takes the hub's position (`outbox.adopt_after_loss`: its unacknowledged items move behind it, audit
  `runner.alert` `outbox_conflict_recovered` with the number of lost items, log `runner.alert
  outbox_conflict_recovered pushed=P hub=L lost=N`) and pushes on. Every other conflict is real (two devices with one
  identity: it comes again or after answered pushes; a store from an old backup: far behind) and fatal:
  `sync_fatal` is persisted, `runner.sync conflict_fatal reason=<why>` and `runner.alert origin_seq_conflict` are
  logged, the metric flag `fatal` is set and `SyncFatal` stops the `sync` task (sessions keep running; an operator
  clears it).
- Transport errors (unreachable hub, answers that do not verify) and other refusals are retried by `run` with
  backoff 1 s -> 30 s (±20 %); `429` waits for `Retry-After`.

While the hub's position is unknown (fresh or wiped store) `push_once` asks `GET /v1/device/state` for
`last_origin_seq` and adopts it (`outbox.adopt_hub_position`); a device that is not `active` yet sends nothing.

Scope queues (KOR-08, ADR-047): an event that cannot be sealed (no key of the needed epoch, no verified log head)
holds back only its own scope. Before a batch is taken every scope queue whose first item can be sealed now is
released (`runner.sync scope_released`); when the walk over the batch meets an event it cannot seal, that event and
the later items of its scope move into the scope's queue (`runner.sync scope_held`) and the walk starts again, so the
items of other scopes go out at once. Queues of scopes the device may read no more are discarded by the reconcile
step of this area (`sync.reconcile`).
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from agent_runner.crypto.errors import CryptoError
from agent_runner.sync import outbox
from agent_runner.sync.backoff import Backoff
from agent_runner.sync.codes import HUB_ERROR_CODES
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError, encode_body
from agent_runner.sync.seal import Sealer

MAX_ITEMS = 500
MAX_BATCH_BYTES = 4 * 1024 * 1024
MIN_INTERVAL_S = 0.1
"""Start-to-start push interval (03-api.md section 6.1; the hub's pacer admits this rate with a reserve)."""
HEDGE_MIN_S = 0.15
"""Shortest wait before an unanswered push is sent once more (ADR-050)."""
HEDGE_FACTOR = 3.0
HEDGE_WINDOW = 16
"""The hedge waits `HEDGE_FACTOR` times the median time to an answer of the last `HEDGE_WINDOW` answered pushes."""
HEDGE_SPACING_S = 2.0
"""At most one copy within this time: a hedged push takes longer than the 100 ms interval, so the copies never spend
more of the hub's pacer than the starts they replace (ADR-050)."""
HEDGE_MAX_BYTES = 256 * 1024
"""A larger batch is not copied: its upload alone may outlast the wait and would always be sent twice."""
IDLE_POLL_S = 5.0
WAITING_REMIND_S = 3600.0
"""A scope queue that stays is logged again (`scope_waiting`) at the first push after a start and then hourly."""
OUTBOX_TARGET = "/v1/device/outbox"
STATE_TARGET = "/v1/device/state"
CONFLICT = "origin_seq_conflict"
CORRUPT = "outbox_corrupt"
ALERT_STATUSES = frozenset({400, 413})
"""Batch-level refusals that repeat on every retry of the same batch: retried, but each raises `runner.alert`."""
GAP = "origin_seq_gap"
LOSS_LIMIT = MAX_ITEMS
"""The most items a power cut may have taken from the store for a conflict to be repaired (HOT-25): the commits of the
last moments, not the hours of an old backup."""
STATUSES = frozenset({"ok", "replayed", "rejected", "reseal"})
_ENVELOPE_BYTES = len(b'{"items":[]}')


class SyncFatal(Exception):
    """Pushing must stop until an operator acts (`code` is `origin_seq_conflict` or `origin_seq_gap`)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PushOutcome:
    """`sent` items in the batch; `more` when another batch is due at once; `waiting` names why nothing was sent."""

    sent: int = 0
    acked_through: int | None = None
    more: bool = False
    waiting: str | None = None


@dataclass
class _Batch:
    wire: list[dict[str, Any]] = field(default_factory=list)
    epochs: dict[int, int] = field(default_factory=dict)
    waiting: str | None = None
    unsealable: outbox.StoredItem | None = None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Pusher:
    def __init__(
        self,
        *,
        pool: Any,
        client: HubClient,
        storage_key: bytes,
        sealer: Sealer,
        log: TextIO,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        backoff: Backoff | None = None,
        max_items: int = MAX_ITEMS,
        max_bytes: int = MAX_BATCH_BYTES,
        min_interval: float = MIN_INTERVAL_S,
        hedge_min: float | None = HEDGE_MIN_S,
        hedge_timer: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pool = pool
        self.client = client
        self.storage_key = storage_key
        self.sealer = sealer
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.backoff = backoff or Backoff()
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.min_interval = min_interval
        # the hedge runs on real time even where tests drive `clock`/`sleep`: it races a request on the wire
        self.hedge_min = hedge_min
        self.hedge_timer = hedge_timer
        self.monotonic = monotonic
        self._answer_times: deque[float] = deque(maxlen=HEDGE_WINDOW)
        self._last_hedge: float | None = None
        # origin_seq -> refused epoch of items the hub answered with `reseal` (lost on restart: the hub asks again).
        self.reseal: dict[int, int] = {}
        self.fatal: str | None = None
        # an `origin_seq_conflict` may be repaired only before the first answered push of this process, and once
        self.conflict_repairable = True
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self._last_start: float | None = None
        # (scope, scope_id) -> clock time the waiting queue was last logged
        self._waiting_logged: dict[tuple[str, str], float] = {}

    def line(self, text: str) -> None:
        self.log.write(f"runner.sync {text}\n")
        self.log.flush()

    def notify(self) -> None:
        """Wake the push loop (new items committed, reconnect)."""
        self.wake.set()

    # --- one batch ---------------------------------------------------------------------------------------------

    async def _fetch_position(self, conn: Any) -> bool:
        state = await self.client.request("GET", STATE_TARGET)
        last = _int(state.get("last_origin_seq"))
        if state.get("state") != "active" or last is None or last < 0:
            self.line(f"position_unavailable state={'active' if state.get('state') == 'active' else 'other'}")
            return False
        if await outbox.adopt_hub_position(conn, self.storage_key, last):
            self.line(f"position_adopted last_origin_seq={last}")
        return True

    async def _wire_body(self, conn: Any, item: outbox.StoredItem, batch: _Batch) -> dict[str, Any] | None:
        if item.item_kind != outbox.EVENT:
            return item.body
        refused = self.reseal.get(item.origin_seq)
        sealed: dict[str, Any] | None = item.sealed
        if sealed is None or refused is not None:
            sealed = await self.sealer.seal(conn, item.body, min_epoch=1 if refused is None else refused + 1)
            if sealed is None:
                batch.unsealable = item
                return None
            await outbox.store_sealed(conn, item.origin_seq, sealed)
            self.reseal.pop(item.origin_seq, None)
        header = sealed["header"]
        assert isinstance(header, dict)
        batch.epochs[item.origin_seq] = int(header["epoch"])
        return {"event": sealed}

    async def _release(self, conn: Any) -> None:
        """Release every scope queue whose first item can be sealed now (it keeps the sealed bytes)."""
        for scope, scope_id, count in await outbox.held_scopes(conn):
            first = await outbox.first_held(conn, self.storage_key, scope, scope_id)
            if first is None:
                continue  # discarded meanwhile
            assert first.item_kind == outbox.EVENT  # a queue starts with the event that could not be sealed
            sealed = await self.sealer.seal(conn, first.body, min_epoch=first.min_epoch)
            if sealed is None:
                self._remind(scope, scope_id, count)
                continue
            self._waiting_logged.pop((scope, scope_id), None)
            released = await outbox.release(conn, self.storage_key, scope, scope_id, first=first.held_id, sealed=sealed)
            await conn.commit()
            self.line(f"scope_released scope={scope} scope_id={scope_id} items={released}")

    def _remind(self, scope: str, scope_id: str, count: int) -> None:
        """Log a queue that still waits: once after a start, then every `WAITING_REMIND_S`."""
        now = self.clock()
        last = self._waiting_logged.get((scope, scope_id))
        if last is None or now - last >= WAITING_REMIND_S:
            self._waiting_logged[(scope, scope_id)] = now
            self.line(f"scope_waiting scope={scope} scope_id={scope_id} items={count}")

    async def _hold(self, conn: Any, item: outbox.StoredItem) -> None:
        """Move an event that cannot be sealed, with the later items of its scope, into the scope's queue."""
        refused = self.reseal.get(item.origin_seq)
        min_epoch = 1 if refused is None else refused + 1
        scope, scope_id, moved = await outbox.hold(conn, self.storage_key, item.origin_seq, min_epoch=min_epoch)
        await conn.commit()
        # the numbers from this item on changed: a refused epoch noted for them belongs to the moved event
        self.reseal = {seq: epoch for seq, epoch in self.reseal.items() if seq < item.origin_seq}
        self._waiting_logged[(scope, scope_id)] = self.clock()
        self.line(f"scope_held scope={scope} scope_id={scope_id} items={moved}")

    async def _walk(self, conn: Any, pushed: int) -> _Batch:
        """Seal and size one batch after `pushed`; stops at the first event that cannot be sealed."""
        batch = _Batch()
        items = await outbox.load_batch(conn, self.storage_key, pushed, self.max_items)
        size = _ENVELOPE_BYTES
        for item in items:
            body = await self._wire_body(conn, item, batch)
            if body is None:
                break
            entry = {"origin_seq": item.origin_seq, "item_kind": item.item_kind, "body": body}
            grow = len(encode_body(entry)) + (1 if batch.wire else 0)
            if batch.wire and size + grow > self.max_bytes:
                break
            batch.wire.append(entry)
            size += grow
        if not items:
            batch.waiting = "idle"
        return batch

    async def _prepare(self) -> tuple[int, _Batch]:
        async with self.pool.connection() as conn:
            fatal = await outbox.sync_fatal(conn)
            if fatal is not None:
                self.fatal = fatal
                raise SyncFatal(fatal)
            pushed = await outbox.pushed_origin_seq(conn)
            if pushed is None:
                if not await self._fetch_position(conn):
                    return 0, _Batch(waiting="position_unknown")
                await conn.commit()
                pushed = await outbox.pushed_origin_seq(conn)
                assert pushed is not None
            try:
                await self._release(conn)
                batch = await self._walk(conn, pushed)
                while batch.unsealable is not None:
                    await self._hold(conn, batch.unsealable)
                    batch = await self._walk(conn, pushed)
            except CryptoError:
                corrupt = True
            else:
                corrupt = False
            if corrupt:
                await conn.rollback()
                raise await self._fail(CORRUPT)
        return pushed, batch

    async def _rate_limit(self) -> bool:
        """Wait until `min_interval` after the start of the previous push (nothing when that has passed: a push that
        took longer than the interval is followed at once). True when it waited."""
        if self._last_start is not None:
            due = self._last_start + self.min_interval - self.clock()
            if due > 0:
                await self.sleep(due)
                return True
        return False

    async def _fail(self, code: str) -> SyncFatal:
        self.fatal = code
        async with self.pool.connection() as conn:
            await outbox.set_state(conn, outbox.FATAL, code)
        self.line(f"alert code={code}")
        self.log.write(f"runner.alert {code}\n")
        self.log.flush()
        return SyncFatal(code)

    async def _gap(self, refusal: HubRefusal, pushed: int) -> PushOutcome:
        expected = _int(refusal.detail.get("expected"))
        missing: int | None = None
        async with self.pool.connection() as conn:
            last = await outbox.last_origin_seq(conn)
            if expected is None or not 1 <= expected <= last + 1:
                fatal = True
            elif expected > last or await outbox.stored(conn, expected):
                fatal = False
                await outbox.rewind(conn, expected)
            else:
                fatal, missing = False, expected
        if fatal:
            raise await self._fail(GAP)
        if missing is not None:
            return await self._realign(missing)
        self.line(f"gap pushed={pushed} expected={expected}")
        return PushOutcome(more=True)

    async def _realign(self, expected: int) -> PushOutcome:
        """The hub asks for an item that was pruned (KOR-10). One refusal is not enough to give items up: the hub's
        own position (`GET /v1/device/state`) must confirm it (`last_origin_seq == expected - 1`), else the push is
        retried like a transport error."""
        state = await self.client.request("GET", STATE_TARGET)
        if state.get("state") != "active" or _int(state.get("last_origin_seq")) != expected - 1:
            self.line(f"gap_unconfirmed expected={expected}")
            raise HubTransportError("position_unconfirmed")
        async with self.pool.connection() as conn:
            try:
                lost = await outbox.realign(conn, self.storage_key, expected)
            except CryptoError:
                await conn.rollback()
                corrupt = True
            else:
                corrupt = False
        if corrupt:
            raise await self._fail(CORRUPT)
        # numbers from `expected` on moved: a pending reseal named an item that has another number now
        self.reseal.clear()
        if lost is not None:
            self.log.write(f"runner.alert {outbox.LOST_ALERT} expected={expected} lost={lost}\n")
            self.log.flush()
        return PushOutcome(more=True)

    async def _conflict(self, pushed: int, first: int) -> PushOutcome:
        """`origin_seq_conflict` for the batch starting at `first`: repaired when it is a lost tail, else fatal."""
        reason, hub_last = await self._conflict_reason(pushed, first)
        if reason is None:
            async with self.pool.connection() as conn:
                try:
                    lost = await outbox.adopt_after_loss(conn, self.storage_key, hub_last)
                except CryptoError:
                    await conn.rollback()
                    lost, corrupt = None, True
                else:
                    corrupt = False
            if corrupt:
                raise await self._fail(CORRUPT)
            reason = "not_contiguous" if lost is None else None
        if reason is not None:
            self.line(f"conflict_fatal reason={reason}")
            raise await self._fail(CONFLICT)
        self.conflict_repairable = False
        self.reseal.clear()  # numbers moved: a pending reseal named an item that has another number now
        self.log.write(f"runner.alert {outbox.CONFLICT_ALERT} pushed={pushed} hub={hub_last} lost={lost}\n")
        self.log.flush()
        return PushOutcome(more=True)

    async def _conflict_reason(self, pushed: int, first: int) -> tuple[str | None, int]:
        """Why the conflict cannot be a lost tail (None when it may be) and the hub's confirmed position."""
        if not self.conflict_repairable:
            return "after_answer", 0
        if first != pushed + 1:
            return "not_contiguous", 0
        state = await self.client.request("GET", STATE_TARGET)
        last = _int(state.get("last_origin_seq"))
        if state.get("state") != "active" or last is None:
            self.line("conflict_unconfirmed")
            raise HubTransportError("position_unconfirmed")
        if last < first:
            return "hub_behind", last
        if last - pushed > LOSS_LIMIT:
            return "too_far", last
        return None, last

    async def push_once(self) -> PushOutcome:
        """Send at most one batch; see the module docstring for the answer handling."""
        async with self.lock:
            pushed, batch = await self._prepare()
            if not batch.wire:
                return PushOutcome(waiting=batch.waiting)
            if await self._rate_limit():
                # taken again after the wait: items committed meanwhile go out now, not one interval later (PERF-03)
                pushed, batch = await self._prepare()
                if not batch.wire:  # its items moved to a scope queue meanwhile
                    return PushOutcome(waiting=batch.waiting)
            self._last_start = self.clock()
            try:
                answer = await self._post({"items": batch.wire})
            except HubRefusal as refusal:
                if refusal.code == CONFLICT:
                    return await self._conflict(pushed, batch.wire[0]["origin_seq"])
                if refusal.code == GAP:
                    return await self._gap(refusal, pushed)
                raise
            return await self._apply(answer, pushed, batch)

    def hedge_after(self) -> float | None:
        """Seconds an unanswered push waits before it is sent once more; None when hedging is off."""
        if self.hedge_min is None:
            return None
        if not self._answer_times:
            return self.hedge_min
        return max(self.hedge_min, HEDGE_FACTOR * statistics.median(self._answer_times))

    def _may_hedge(self, body: dict[str, Any], started: float) -> bool:
        if len(encode_body(body)) > HEDGE_MAX_BYTES:
            return False
        return self._last_hedge is None or started - self._last_hedge >= HEDGE_SPACING_S

    async def _post(self, body: dict[str, Any]) -> dict[str, object]:
        """POST one batch; sent once more when the answer is late (module docstring)."""
        started = self.monotonic()
        after = self.hedge_after() if self._may_hedge(body, started) else None
        first: asyncio.Future[dict[str, object]] = asyncio.ensure_future(
            self.client.request("POST", OUTBOX_TARGET, body)
        )
        requests = [first]
        tasks: list[asyncio.Future[Any]] = [first]
        try:
            if after is not None:
                timer: asyncio.Future[Any] = asyncio.ensure_future(self.hedge_timer(after))
                tasks.append(timer)
                await asyncio.wait((first, timer), return_when=asyncio.FIRST_COMPLETED)
                if not first.done():
                    self._last_hedge = self.monotonic()
                    self.line(f"push_hedged after_ms={round(after * 1000)}")
                    second = asyncio.ensure_future(self.client.request("POST", OUTBOX_TARGET, body))
                    requests.append(second)
                    tasks.append(second)
            answer = await (self._first_answer(requests) if len(requests) > 1 else first)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._answer_times.append(self.monotonic() - started)
        return answer

    @staticmethod
    async def _first_answer(requests: list[asyncio.Future[dict[str, object]]]) -> dict[str, object]:
        """The first answer of the requests; when all fail, the error of the first request."""
        pending: set[asyncio.Future[dict[str, object]]] = set(requests)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in requests:
                if task in done and task.exception() is None:
                    return task.result()
        return requests[0].result()  # raises the first request's error

    async def _apply(self, answer: dict[str, object], pushed: int, batch: _Batch) -> PushOutcome:
        self.conflict_repairable = False
        first, last = batch.wire[0]["origin_seq"], batch.wire[-1]["origin_seq"]
        acked = _int(answer.get("acked_through"))
        results = answer.get("results")
        if acked is None or not pushed <= acked <= last or not isinstance(results, list):
            raise HubTransportError("response_invalid")
        resealed = False
        for result in results:
            seq = _int(result.get("origin_seq")) if isinstance(result, dict) else None
            if seq is None or not first <= seq <= last:
                raise HubTransportError("response_invalid")
            status = result.get("status")
            if status not in STATUSES or (status == "reseal" and seq != acked + 1):
                raise HubTransportError("response_invalid")
            if status == "rejected":
                kind = batch.wire[seq - first]["item_kind"]
                error = result.get("error")
                code = error if isinstance(error, str) and error in HUB_ERROR_CODES else "invalid"
                self.line(f"item_rejected origin_seq={seq} item_kind={kind} error={code}")
            elif status == "reseal" and seq in batch.epochs:
                self.reseal[seq] = batch.epochs[seq]
                resealed = True
                self.line(f"reseal origin_seq={seq} epoch={batch.epochs[seq]}")
        async with self.pool.connection() as conn:
            await outbox.acknowledge(conn, acked)
        more = resealed or (acked > pushed and batch.waiting is None)
        return PushOutcome(sent=len(batch.wire), acked_through=acked, more=more)

    # --- loop ----------------------------------------------------------------------------------------------------

    async def drain(self) -> PushOutcome:
        """Push until nothing more is due at once (reconnect order, shutdown flush); errors propagate."""
        outcome = await self.push_once()
        while outcome.more:
            outcome = await self.push_once()
        return outcome

    async def _wait(self, delay: float | None) -> None:
        """Until `notify()` or `delay` seconds (`IDLE_POLL_S` when None)."""
        waiter = asyncio.ensure_future(self.wake.wait())
        sleeper = asyncio.ensure_future(self.sleep(IDLE_POLL_S if delay is None else delay))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            sleeper.cancel()
            await asyncio.gather(waiter, sleeper, return_exceptions=True)

    async def run(self, stopping: asyncio.Event) -> None:
        """Push whenever items are due until `stopping`; returns on `SyncFatal` after logging it. After a refused or
        failed push the loop waits the whole `Retry-After` or backoff: new items (`notify()`) end only the idle wait
        (KOR-13; a reconnect drains on its own, `agent_runner.sync.ws`)."""
        while not stopping.is_set():
            delay: float | None = None
            failed = True
            # cleared before the attempt, so a notification during the attempt still ends the next wait at once
            self.wake.clear()
            try:
                outcome = await self.push_once()
            except SyncFatal as fatal:
                self.line(f"stopped code={fatal.code}")
                return
            except HubRefusal as refusal:
                retry = refusal.retry_after if refusal.status == 429 else None
                delay = self.backoff.next() if retry is None else float(retry)
                self.line(f"push_refused status={refusal.status} error={refusal.code}")
                if refusal.status in ALERT_STATUSES:
                    self.log.write(f"runner.alert push_refused_{refusal.status}\n")
                    self.log.flush()
            except HubTransportError as error:
                delay = self.backoff.next()
                self.line(f"push_failed {error.describe()}")
            else:
                failed = False
                self.backoff.reset()
                if outcome.more:
                    continue
                if outcome.waiting == "position_unknown":
                    delay = IDLE_POLL_S
            if failed:
                assert delay is not None
                await self.sleep(delay)
            else:
                await self._wait(delay)
