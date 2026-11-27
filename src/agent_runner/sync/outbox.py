"""Runner outbox (03-api.md section 6.1, 02-data-model.md section 4, 05-runner.md section 2.3).

`enqueue(conn, item_kind, body)` runs **inside the caller's transaction** of the state change the item describes, so
the item exists exactly when the change is committed. `origin_seq` is gapless: the writer takes
`pg_advisory_xact_lock(742180201)` (held until its commit or rollback) and inserts `MAX(origin_seq) + 1` (pattern of the
legacy `agent_cli.store` `origin_seq`), never below `pushed_origin_seq + 1` (a wiped store that adopted the hub's
position before it wrote any item continues there, DR-01). A rolled-back writer releases the lock without leaving a
number behind. The insert sends `NOTIFY runner_outbox`, delivered after the commit, which wakes the push loop.

The body is stored as plaintext JSON sealed with the storage key (`crypto.storage.seal_local`, purpose `outbox`,
context `<origin_seq>:<item_kind>`). An `event` item stores the **draft** of a device-authored event: header fields
without `epoch`, `author`, `lh`, `salt`, `nonce`, plus the plaintext body. It is sealed at its first push attempt
(`sync.seal`) and the sealed event is stored in `sealed` and reused for every retry (04-crypto.md section 5.12).

`sync_state` keys: `pushed_origin_seq` (advanced only by the hub's `acked_through`, set by `adopt_hub_position`, moved
back by `rewind` and `realign` for an `origin_seq_gap`),
`hub_ws_last_connect`, `sync_fatal` (an `origin_seq_conflict` that is not a lost tail, `adopt_after_loss`, stops
pushing until an operator clears it).

After a store wipe the numbering must continue at the hub's `last_origin_seq + 1`. Until `pushed_origin_seq` is known
the push loop sends nothing; `adopt_hub_position(conn, last_origin_seq)` (called with the value of
`GET /v1/device/state`, by the reconcile step of RUN-03 or by the push loop itself) renumbers the items written
meanwhile to follow the hub's position and records it.

Scope queues (KOR-08, ADR-047): an event the device cannot seal yet (no key of the needed epoch, no verified log head)
must not hold back the items of other scopes. The push loop moves it with every later item of its scope (`event` items
with the same `scope`/`scope_id`, and `operation_transition` items naming one of their IDs as `result_event_id`, which
the hub accepts only after the result event) into `runner.outbox_held` (`hold`) and renumbers the remaining later items
to close the gap. Only items the hub never stored are moved: the push walk stops at the first item it cannot seal, so
nothing at or behind it was ever sent, except after a `reseal` answer, which says that the hub stored nothing from
that item on. While a scope has a queue, `enqueue` appends its new items (and transitions naming a queued result) to the
queue without a number, so the order within a scope is kept. `release` gives the queue its numbers after the current
last item once the first item can be sealed; `discard` drops a queue whose scope the device may read no more (the
caller writes the audit, `sync.reconcile`). Every change of the queues runs under the outbox lock.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from agent_runner.crypto.storage import open_local, outbox_context, seal_local
from agent_runner.sync.codes import ITEM_KINDS
from agent_runner.sync.seal import check_draft

LOCK_ID = 742180201
CHANNEL = "runner_outbox"
PUSHED = "pushed_origin_seq"
WS_LAST_CONNECT = "hub_ws_last_connect"
FATAL = "sync_fatal"
EVENT = "event"
OPERATION_TRANSITION = "operation_transition"
DRAFT_HEADER_KEYS = frozenset({"id", "org", "scope", "scope_id", "wg", "task", "session", "kind", "ref", "ts"})
_STATE_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


@dataclass(frozen=True)
class StoredItem:
    """One outbox row with its opened body; `sealed` is the stored sealed event of an `event` item."""

    origin_seq: int
    item_kind: str
    body: dict[str, Any]
    sealed: dict[str, Any] | None


@dataclass(frozen=True)
class HeldItem:
    """The first item of a scope queue with its opened body and the lowest epoch it may be sealed with."""

    held_id: int
    item_kind: str
    body: dict[str, Any]
    min_epoch: int


def held_context(held_id: int, item_kind: str) -> str:
    """Storage context of `outbox_held.body_ct`: `held-<id>:<item_kind>` (never equal to a numbered item's context)."""
    return f"held-{held_id}:{item_kind}"


def event_scope(body: Mapping[str, Any]) -> tuple[str, str]:
    """`(scope, scope_id)` of an event draft."""
    header = body["header"]
    return str(header["scope"]), str(header["scope_id"])


def _result_event_id(body: Mapping[str, Any]) -> str | None:
    """The `result_event_id` of an `operation_transition` body when it is a UUID."""
    value = body.get("result_event_id")
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _json(body: Mapping[str, object]) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False, sort_keys=True).encode()


def check_item(item_kind: str, body: object) -> dict[str, Any]:
    """The body as a dict; ValueError for an unknown kind, a non-object body or a draft without the draft shape,
    `CryptoError` for a draft whose header or size could never be sealed (`seal.check_draft`)."""
    if item_kind not in ITEM_KINDS:
        raise ValueError(f"unknown outbox item kind: {item_kind!r}")
    if not isinstance(body, dict):
        raise ValueError("an outbox body must be a JSON object")
    if item_kind == EVENT:
        header, plain = body.get("header"), body.get("body")
        if set(body) != {"header", "body"} or not isinstance(header, dict) or not isinstance(plain, dict):
            raise ValueError("an event draft is {header, body}")
        if set(header) != DRAFT_HEADER_KEYS:
            raise ValueError("an event draft header has exactly the keys of DRAFT_HEADER_KEYS")
        check_draft(body)
    return body


def event_draft(
    *,
    id: str,
    org: str,
    scope: str,
    scope_id: str,
    kind: str,
    ts: str,
    body: Mapping[str, object],
    wg: str = "",
    task: str = "",
    session: str = "",
    ref: str = "",
) -> dict[str, Any]:
    """The outbox body of an `event` item (the seal step adds `epoch`, `author` and `lh`)."""
    header = {
        "id": id,
        "org": org,
        "scope": scope,
        "scope_id": scope_id,
        "wg": wg,
        "task": task,
        "session": session,
        "kind": kind,
        "ref": ref,
        "ts": ts,
    }
    return {"header": header, "body": dict(body)}


async def _lock(conn: AsyncConnection[Any]) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))


async def _queue_of(conn: AsyncConnection[Any], item_kind: str, body: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """`(scope, scope_id, event_id)` of the scope queue a new item has to join, None when it is numbered at once."""
    if item_kind == EVENT:
        scope, scope_id = event_scope(body)
        cur = await conn.execute(
            "SELECT 1 FROM runner.outbox_held WHERE scope = %s AND scope_id = %s LIMIT 1", (scope, scope_id)
        )
        return None if await cur.fetchone() is None else (scope, scope_id, str(body["header"]["id"]))
    result = _result_event_id(body) if item_kind == OPERATION_TRANSITION else None
    if result is None:
        return None
    cur = await conn.execute(
        "SELECT scope, scope_id::text FROM runner.outbox_held WHERE event_id = %s AND item_kind = %s LIMIT 1",
        (result, EVENT),
    )
    row = await cur.fetchone()
    return None if row is None else (str(row[0]), str(row[1]), result)


async def _insert_held(
    conn: AsyncConnection[Any],
    storage_key: bytes,
    queue: tuple[str, str, str],
    item_kind: str,
    raw: bytes,
    *,
    sealed: object = None,
    min_epoch: int = 1,
    created_at: datetime | None = None,
) -> None:
    cur = await conn.execute("SELECT nextval(pg_get_serial_sequence('runner.outbox_held', 'id'))")
    row = await cur.fetchone()
    assert row is not None
    held_id = int(row[0])
    blob = seal_local(storage_key, "outbox", held_context(held_id, item_kind), raw)
    scope, scope_id, event_id = queue
    await conn.execute(
        "INSERT INTO runner.outbox_held (id, scope, scope_id, item_kind, event_id, body_ct, sealed, min_epoch,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))",
        (held_id, scope, scope_id, item_kind, event_id, blob, sealed, min_epoch, created_at),
    )


async def _next_seq(conn: AsyncConnection[Any]) -> int:
    # Never below the adopted hub position: a wiped store adopts it while its outbox may still be empty (DR-01).
    cur = await conn.execute(
        "SELECT GREATEST(COALESCE(MAX(origin_seq), 0),"
        " COALESCE((SELECT value::bigint FROM runner.sync_state WHERE key = %s), 0)) + 1 FROM runner.outbox",
        (PUSHED,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def enqueue(conn: AsyncConnection[Any], storage_key: bytes, item_kind: str, body: Mapping[str, object]) -> int:
    """Append one item in the caller's transaction; returns its `origin_seq`, or 0 (no number yet) when the item joined
    the queue of its scope (it is numbered when the queue is released, see the module docstring).

    `conn` must be inside a transaction (`async with conn.transaction():`, or a non-autocommit connection that the
    caller commits); in autocommit mode outside a transaction block the advisory lock would end with the statement
    and two writers could take the same number, so that is refused with ValueError.
    """
    if conn.autocommit and conn.info.transaction_status != TransactionStatus.INTRANS:
        raise ValueError("enqueue needs an open transaction")
    checked = check_item(item_kind, body)
    raw = _json(checked)
    await _lock(conn)
    queue = await _queue_of(conn, item_kind, checked)
    if queue is not None:
        await _insert_held(conn, storage_key, queue, item_kind, raw)
        return 0
    seq = await _next_seq(conn)
    blob = seal_local(storage_key, "outbox", outbox_context(seq, item_kind), raw)
    await conn.execute(
        "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct) VALUES (%s, %s, %s)", (seq, item_kind, blob)
    )
    await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
    return seq


def open_item(storage_key: bytes, origin_seq: int, item_kind: str, blob: bytes) -> dict[str, Any]:
    """The plaintext body of a stored item (`decrypt_failed` when the row was tampered with)."""
    body = json.loads(open_local(storage_key, "outbox", outbox_context(origin_seq, item_kind), blob))
    assert isinstance(body, dict)
    return body


async def get_state(conn: AsyncConnection[Any], key: str) -> str | None:
    cur = await conn.execute("SELECT value FROM runner.sync_state WHERE key = %s", (key,))
    row = await cur.fetchone()
    return None if row is None else str(row[0])


async def set_state(conn: AsyncConnection[Any], key: str, value: str) -> None:
    if not _STATE_KEY_RE.fullmatch(key):
        raise ValueError("sync_state key must be lower snake case")
    await conn.execute(
        "INSERT INTO runner.sync_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


async def delete_state(conn: AsyncConnection[Any], key: str) -> None:
    await conn.execute("DELETE FROM runner.sync_state WHERE key = %s", (key,))


async def pushed_origin_seq(conn: AsyncConnection[Any]) -> int | None:
    """The highest `origin_seq` the hub acknowledged; None while the hub's position is unknown (fresh store)."""
    value = await get_state(conn, PUSHED)
    return None if value is None else int(value)


async def sync_fatal(conn: AsyncConnection[Any]) -> str | None:
    """The fatal sync condition (`origin_seq_conflict`, ...) that stopped pushing, None when syncing is allowed."""
    return await get_state(conn, FATAL)


async def clear_fatal(conn: AsyncConnection[Any]) -> None:
    """Operator action after an investigated fatal condition (05-runner.md section 2.3)."""
    await delete_state(conn, FATAL)


async def last_origin_seq(conn: AsyncConnection[Any]) -> int:
    cur = await conn.execute("SELECT COALESCE(MAX(origin_seq), 0) FROM runner.outbox")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def backlog(conn: AsyncConnection[Any]) -> int:
    """Number of numbered items not yet acknowledged by the hub (all items while the hub position is unknown).

    Items waiting in scope queues are not counted: the metrics area holds its samples back while the backlog is high
    (`metrics.outbox.BACKLOG_LIMIT`), and a waiting scope must not hold back the device's other items (ADR-047)."""
    pushed = await pushed_origin_seq(conn)
    cur = await conn.execute("SELECT count(*) FROM runner.outbox WHERE origin_seq > %s", (pushed or 0,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def adopt_hub_position(conn: AsyncConnection[Any], storage_key: bytes, hub_last: int) -> bool:
    """Record the hub's `last_origin_seq` while the local position is unknown (fresh or wiped store).

    Items written before the position was known are renumbered to follow `hub_last` (their bodies are re-sealed for
    the new context; none of them was pushed, so none is sealed). Returns True when the position was adopted, False
    when it was already known (the known position is authoritative; later values are ignored).
    """
    if isinstance(hub_last, bool) or not isinstance(hub_last, int) or hub_last < 0:
        raise ValueError("last_origin_seq must be a non-negative integer")
    async with conn.transaction():
        await _lock(conn)
        if await pushed_origin_seq(conn) is not None:
            return False
        cur = await conn.execute("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq DESC")
        rows = await cur.fetchall()
        # Highest first, so a moved row never collides with one that has not moved yet.
        for seq, kind, blob in rows if hub_last > 0 else []:
            body = open_item(storage_key, seq, kind, bytes(blob))
            new_seq = seq + hub_last
            new_blob = seal_local(storage_key, "outbox", outbox_context(new_seq, kind), _json(body))
            await conn.execute(
                "UPDATE runner.outbox SET origin_seq = %s, body_ct = %s WHERE origin_seq = %s", (new_seq, new_blob, seq)
            )
        await set_state(conn, PUSHED, str(hub_last))
    return True


async def load_batch(conn: AsyncConnection[Any], storage_key: bytes, after: int, limit: int) -> list[StoredItem]:
    """Up to `limit` items with `origin_seq > after`, ascending, bodies opened."""
    cur = await conn.execute(
        "SELECT origin_seq, item_kind, body_ct, sealed FROM runner.outbox WHERE origin_seq > %s "
        "ORDER BY origin_seq LIMIT %s",
        (after, limit),
    )
    items: list[StoredItem] = []
    for seq, kind, blob, sealed in await cur.fetchall():
        items.append(StoredItem(int(seq), str(kind), open_item(storage_key, seq, kind, bytes(blob)), sealed))
    return items


async def store_sealed(conn: AsyncConnection[Any], origin_seq: int, sealed: Mapping[str, object]) -> None:
    """Store (or, after `reseal`, replace) the sealed event of an `event` item."""
    await conn.execute(
        "UPDATE runner.outbox SET sealed = %s WHERE origin_seq = %s AND item_kind = %s",
        (Jsonb(dict(sealed)), origin_seq, EVENT),
    )


async def acknowledge(conn: AsyncConnection[Any], acked_through: int) -> None:
    """Advance `pushed_origin_seq` to `acked_through` (never backwards) and stamp `pushed_at`."""
    pushed = await pushed_origin_seq(conn) or 0
    if acked_through <= pushed:
        return
    await conn.execute(
        "UPDATE runner.outbox SET pushed_at = now() WHERE origin_seq > %s AND origin_seq <= %s",
        (pushed, acked_through),
    )
    await set_state(conn, PUSHED, str(acked_through))


async def rewind(conn: AsyncConnection[Any], expected: int) -> None:
    """`origin_seq_gap`: the hub expects `expected` next, so the next push starts there (under the outbox lock, so a
    concurrent `prune` sees the rewound position)."""
    await _lock(conn)
    await set_state(conn, PUSHED, str(expected - 1))


LOST_ALERT = "outbox_items_lost"


async def realign(conn: AsyncConnection[Any], storage_key: bytes, expected: int) -> int | None:
    """`origin_seq_gap` for a number the outbox no longer holds (KOR-10, AUD-02 NA-02): a hub restored from an older
    export expects `expected`, but that item was pruned (acknowledged by the hub before the restore, older than
    `RETAIN_S`). It cannot be resent. The stored items from `expected` on, which the restored hub never saw, are
    renumbered without gaps from `expected` (ascending, each to a free number at or below its own; their sealed events
    are kept, the seal does not bind `origin_seq`), `pushed_origin_seq` becomes `expected - 1` and one audit
    `runner.alert` `{alert: "outbox_items_lost", value: <numbers in [expected, last] no longer stored>}` is appended
    behind them. Caller's transaction, under the outbox lock; returns the number of lost items, or None when there is
    nothing to realign (`expected` is stored or above the last item: a plain `rewind`). `CryptoError` for a stored
    item that does not open."""
    await _lock(conn)
    last = await last_origin_seq(conn)
    if not 1 <= expected <= last:
        return None
    cur = await conn.execute(
        "SELECT origin_seq, item_kind, body_ct FROM runner.outbox WHERE origin_seq >= %s ORDER BY origin_seq",
        (expected,),
    )
    rows = await cur.fetchall()
    if int(rows[0][0]) == expected:
        return None
    for new_seq, (seq, kind, blob) in enumerate(rows, start=expected):
        raw = _json(open_item(storage_key, seq, kind, bytes(blob)))
        await conn.execute(
            "UPDATE runner.outbox SET origin_seq = %s, body_ct = %s WHERE origin_seq = %s",
            (new_seq, seal_local(storage_key, "outbox", outbox_context(new_seq, kind), raw), seq),
        )
    await set_state(conn, PUSHED, str(expected - 1))
    lost = last - expected + 1 - len(rows)
    alert = {
        "action": "runner.alert",
        "result": "failed",
        "target_kind": "outbox",
        "target_id": "origin_seq_gap",
        "detail": {"alert": LOST_ALERT, "value": lost},
    }
    await enqueue(conn, storage_key, "audit", alert)
    return lost


CONFLICT_ALERT = "outbox_conflict_recovered"


async def adopt_after_loss(conn: AsyncConnection[Any], storage_key: bytes, hub_last: int) -> int | None:
    """`origin_seq_conflict` after the store lost its newest commits in a power cut (HOT-25): the hub holds items up
    to `hub_last` that the store no longer has, and the store gave their numbers to new items. Every item after
    `pushed_origin_seq` moves by `hub_last - pushed_origin_seq` (highest first, bodies re-sealed for the new context;
    the sealed events are kept, the seal does not bind `origin_seq`), `pushed_origin_seq` becomes `hub_last` and one
    audit `runner.alert` `{alert: "outbox_conflict_recovered", value: <the hub's items the store lost>}` is appended
    behind them. Caller's transaction, under the outbox lock. Returns that number, or None when the store does not
    look like a lost tail (position unknown, `hub_last` not ahead of it, or the first item after it is not
    `pushed_origin_seq + 1`): then the conflict is real. `CryptoError` for a stored item that does not open."""
    await _lock(conn)
    pushed = await pushed_origin_seq(conn)
    if pushed is None or hub_last <= pushed:
        return None
    cur = await conn.execute(
        "SELECT origin_seq, item_kind, body_ct FROM runner.outbox WHERE origin_seq > %s ORDER BY origin_seq DESC",
        (pushed,),
    )
    rows = await cur.fetchall()
    if not rows or int(rows[-1][0]) != pushed + 1:
        return None
    shift = hub_last - pushed
    for seq, kind, blob in rows:
        new_seq = int(seq) + shift
        raw = _json(open_item(storage_key, seq, kind, bytes(blob)))
        await conn.execute(
            "UPDATE runner.outbox SET origin_seq = %s, body_ct = %s WHERE origin_seq = %s",
            (new_seq, seal_local(storage_key, "outbox", outbox_context(new_seq, kind), raw), seq),
        )
    await set_state(conn, PUSHED, str(hub_last))
    alert = {
        "action": "runner.alert",
        "result": "failed",
        "target_kind": "outbox",
        "target_id": "origin_seq_conflict",
        "detail": {"alert": CONFLICT_ALERT, "value": shift},
    }
    await enqueue(conn, storage_key, "audit", alert)
    return shift


async def stored(conn: AsyncConnection[Any], origin_seq: int) -> bool:
    cur = await conn.execute("SELECT 1 FROM runner.outbox WHERE origin_seq = %s", (origin_seq,))
    return await cur.fetchone() is not None


async def first_origin_seq(conn: AsyncConnection[Any]) -> int | None:
    """The lowest stored `origin_seq` (items below it were pruned or never existed); None for an empty outbox."""
    cur = await conn.execute("SELECT MIN(origin_seq) FROM runner.outbox")
    row = await cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


# -- retention (KOR-03, AUD-01 B-19; ADR-038) --------------------------------------------------------------------

RETAIN_S = 3600
"""Acknowledged items stay this long: the final text of a subagent is read from the event items of the last hour
(ADR-038), and a recently pushed item is still at hand for an operator."""
PRUNE_BATCH = 5000


async def prune(conn: AsyncConnection[Any], *, retain_s: float = RETAIN_S, batch: int = PRUNE_BATCH) -> int:
    """Delete items the hub acknowledged (`origin_seq <= pushed_origin_seq`) that are older than `retain_s`, at most
    `batch` per call; returns the number deleted. Nothing while the hub's position is unknown. The item with the
    highest `origin_seq` always stays, so the numbering (`MAX + 1`) never goes back. Only a prefix goes: nothing above
    the lowest item that is still younger than `retain_s` (`created_at` is the writer's transaction start and does not
    follow `origin_seq`), so the stored items never have a hole and a hub restored below them misses only the range
    under the lowest one (`realign`, KOR-10). Runs under the outbox lock in the caller's transaction, like `enqueue`."""
    await _lock(conn)
    pushed = await pushed_origin_seq(conn)
    if pushed is None:
        return 0
    cur = await conn.execute(
        "DELETE FROM runner.outbox WHERE origin_seq IN (SELECT origin_seq FROM runner.outbox WHERE origin_seq <= %s"
        " AND origin_seq < (SELECT MAX(origin_seq) FROM runner.outbox)"
        " AND origin_seq < (SELECT COALESCE(MIN(origin_seq), %s + 1) FROM runner.outbox"
        " WHERE created_at >= now() - make_interval(secs => %s))"
        " ORDER BY origin_seq LIMIT %s)",
        (pushed, pushed, retain_s, batch),
    )
    return cur.rowcount


# -- scope queues (KOR-08, ADR-047) -------------------------------------------------------------------------------


async def hold(
    conn: AsyncConnection[Any], storage_key: bytes, origin_seq: int, *, min_epoch: int = 1
) -> tuple[str, str, int]:
    """Move the event at `origin_seq` and every later item of its scope into the scope's queue and renumber the
    remaining later items to close the gap (caller's transaction); returns `(scope, scope_id, items moved)`.

    Only for an item the hub never stored (module docstring). `min_epoch` is the lowest epoch the event may be sealed
    with (one above the epoch the hub refused with `reseal`); its stored sealed bytes are dropped, the sealed bytes of
    the other moved items are kept (they were never stored either)."""
    await _lock(conn)
    cur = await conn.execute(
        "SELECT origin_seq, item_kind, body_ct, sealed, created_at FROM runner.outbox WHERE origin_seq >= %s"
        " ORDER BY origin_seq",
        (origin_seq,),
    )
    rows = await cur.fetchall()
    assert rows
    assert (int(rows[0][0]), rows[0][1]) == (origin_seq, EVENT)
    bodies = [open_item(storage_key, row[0], row[1], bytes(row[2])) for row in rows]
    scope = event_scope(bodies[0])
    events: set[str] = set()
    moved: list[int] = []
    staying: list[tuple[int, str, bytes]] = []
    for (seq, kind, _blob, sealed, created_at), body in zip(rows, bodies, strict=True):
        if kind == EVENT and event_scope(body) == scope:
            event_id = str(body["header"]["id"])
            events.add(event_id)
        elif kind == OPERATION_TRANSITION and _result_event_id(body) in events:
            event_id = str(_result_event_id(body))
        else:
            staying.append((int(seq), str(kind), _json(body)))
            continue
        first = seq == origin_seq
        await _insert_held(
            conn,
            storage_key,
            (scope[0], scope[1], event_id),
            str(kind),
            _json(body),
            sealed=None if first or sealed is None else Jsonb(sealed),
            min_epoch=min_epoch if first else 1,
            created_at=created_at,
        )
        moved.append(int(seq))
    await conn.execute("DELETE FROM runner.outbox WHERE origin_seq = ANY(%s)", (moved,))
    # ascending, each to a number at or below its own: the target is free (moved away or vacated just before)
    for new_seq, (seq, kind, raw) in enumerate(staying, start=origin_seq):
        blob = seal_local(storage_key, "outbox", outbox_context(new_seq, kind), raw)
        await conn.execute(
            "UPDATE runner.outbox SET origin_seq = %s, body_ct = %s WHERE origin_seq = %s", (new_seq, blob, seq)
        )
    return scope[0], scope[1], len(moved)


async def held_scopes(conn: AsyncConnection[Any]) -> list[tuple[str, str, int]]:
    """`(scope, scope_id, items)` of every scope queue, the oldest queue first."""
    cur = await conn.execute(
        "SELECT scope, scope_id::text, count(*) FROM runner.outbox_held GROUP BY scope, scope_id ORDER BY min(id)"
    )
    return [(str(a), str(b), int(c)) for a, b, c in await cur.fetchall()]


async def first_held(conn: AsyncConnection[Any], storage_key: bytes, scope: str, scope_id: str) -> HeldItem | None:
    """The oldest item of a scope queue, None when the queue is empty."""
    cur = await conn.execute(
        "SELECT id, item_kind, body_ct, min_epoch FROM runner.outbox_held WHERE scope = %s AND scope_id = %s"
        " ORDER BY id LIMIT 1",
        (scope, scope_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    held_id, kind = int(row[0]), str(row[1])
    body = json.loads(open_local(storage_key, "outbox", held_context(held_id, kind), bytes(row[2])))
    return HeldItem(held_id=held_id, item_kind=kind, body=body, min_epoch=int(row[3]))


async def release(
    conn: AsyncConnection[Any],
    storage_key: bytes,
    scope: str,
    scope_id: str,
    *,
    first: int,
    sealed: Mapping[str, object],
) -> int:
    """Number the items of a scope queue after the current last item, in their order (caller's transaction); `sealed`
    is stored for the queue's first item when it is still `first` (the item the caller sealed). Returns the number of
    items released."""
    await _lock(conn)
    cur = await conn.execute(
        "SELECT id, item_kind, body_ct, sealed, created_at FROM runner.outbox_held WHERE scope = %s AND scope_id = %s"
        " ORDER BY id",
        (scope, scope_id),
    )
    rows = await cur.fetchall()
    seq = await _next_seq(conn)
    for offset, (held_id, kind, blob, stored, created_at) in enumerate(rows):
        raw = open_local(storage_key, "outbox", held_context(int(held_id), kind), bytes(blob))
        new_blob = seal_local(storage_key, "outbox", outbox_context(seq + offset, kind), raw)
        chosen = Jsonb(dict(sealed)) if int(held_id) == first else (None if stored is None else Jsonb(stored))
        await conn.execute(
            "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct, sealed, created_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (seq + offset, kind, new_blob, chosen, created_at),
        )
    await conn.execute("DELETE FROM runner.outbox_held WHERE id = ANY(%s)", ([int(row[0]) for row in rows],))
    await conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
    return len(rows)


async def discard(conn: AsyncConnection[Any], scope: str, scope_id: str) -> int:
    """Drop a scope queue (caller's transaction, which also writes the audit); returns the number of items."""
    await _lock(conn)
    cur = await conn.execute("DELETE FROM runner.outbox_held WHERE scope = %s AND scope_id = %s", (scope, scope_id))
    return cur.rowcount


async def event_drafts(
    conn: AsyncConnection[Any], storage_key: bytes, since: datetime, batch: int = 256
) -> AsyncGenerator[dict[str, Any]]:
    """Every event draft written since `since`, newest first: the drafts waiting in scope queues (newer than every
    numbered item, a released queue is numbered after the current last item), then the numbered ones. One statement
    through a server-side cursor, so the rows come from one snapshot and a hold or release committed meanwhile can
    neither repeat nor hide a draft (readers of the local drafts: the final text of a subagent, ADR-038)."""
    async with conn.transaction(), conn.cursor(name="outbox_event_drafts") as cur:
        await cur.execute(
            "SELECT 0 AS part, id AS n, body_ct FROM runner.outbox_held WHERE item_kind = %s AND created_at >= %s"
            " UNION ALL SELECT 1, origin_seq, body_ct FROM runner.outbox WHERE item_kind = %s AND created_at >= %s"
            " ORDER BY part, n DESC",
            (EVENT, since, EVENT, since),
        )
        while rows := await cur.fetchmany(batch):
            for part, number, blob in rows:
                context = held_context(int(number), EVENT) if part == 0 else outbox_context(int(number), EVENT)
                yield json.loads(open_local(storage_key, "outbox", context, bytes(blob)))
