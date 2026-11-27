"""Merging of the usage deltas of a session (ADR-086 section 3; 05-runner.md section 10.4).

The guest gateway journals one `usage` record per model request, and each became one outbox item `usage`. The items
of a session are now merged over `WINDOW_S` (10 s) into one: the store keeps one row per session in
`runner.usage_pending` (`item` = the merged outbox body, `first_wall` = device wall clock of its first delta; counts
only, no content) inside the journal page's transaction, so a merged delta is never lost and never counted twice.

- `add(conn, env, item)`: merges `item` into the session's row - the seven `delta` counters are added (capped at the
  largest contract count), every other field is the newest one, exactly what the hub keeps when it applies the items
  one by one ("latest reported values") - and writes the outbox item at once when the first delta is `WINDOW_S` old.
  Only deltas of the same billing key merge: the hub charges an item under its `model` and `access_origin` (KOR-31,
  12-billing.md section 4), books it to the person in its `triggered_by` (KOR-46) and to the subscription in its
  `subscription_id` (ADR-103), prices it with the price list in
  force at its `at` and books it on the UTC day of its `at` (`hub.charge_daily`), so a delta whose `model`,
  `access_origin`, `triggered_by`, `subscription_id` or UTC day of `at` differs from the pending row's writes that row
  out first and
  starts a new one (a delta or row without `at` has no day and merges with either). `at` (the time of the request)
  is the newest merged request's time (a request without a valid time keeps the one before) - the time of the last
  request the item bills, never more than `WINDOW_S` after its first one and
  never on another UTC day. The runner does not know the hub's price list versions (the device state says only
  whether one is in force, `billing.active`), so it cannot split at a price list change; a version whose
  `valid_from` falls inside a window prices at most the `WINDOW_S` of requests before it with the newer list.
- `flush(conn, env, every=False)`: writes the outbox items of the rows whose first delta is `WINDOW_S` old or whose
  session has ended (`stopped`, `failed`, or no longer known), all rows with `every`. The daemon task `usage_merge`
  runs it every `TICK_S`; rows left by a stopped daemon are written by the next one.

Both take the outbox's transaction lock before a row lock (the lock order of `sessions.records`). The totals the hub
counts are unchanged; only the number of items falls (one per session and 10 s instead of one per request). The
platform budget adds the cost of the pending rows (`model.uncounted`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.model.gateways import running_service
from agent_runner.sessions.journal import MAX_COUNT
from agent_runner.sessions.records import FINAL
from agent_runner.sync.outbox import LOCK_ID

WINDOW_S = 10.0
TICK_S = 1.0


def _key(item: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """The billing key besides the session and the day: deltas merge only within one (model, access_origin,
    triggered_by, subscription_id)."""
    return item.get("model"), item.get("access_origin"), item.get("triggered_by"), item.get("subscription_id")


def _day(item: Mapping[str, Any]) -> str | None:
    """The UTC day of the item's `at` (`YYYY-MM-DD`, the runner writes `at` in UTC), None without one."""
    at = item.get("at")
    return at[:10] if isinstance(at, str) else None


def mergeable(row: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Whether `item` may merge into the pending `row` (module docstring: same billing key and UTC day)."""
    days = (_day(row), _day(item))
    return _key(row) == _key(item) and (None in days or days[0] == days[1])


def merged(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """`new` with the deltas of `old` added (module docstring)."""
    delta = {name: min(int(old["delta"].get(name, 0)) + int(value), MAX_COUNT) for name, value in new["delta"].items()}
    kept = {"at": old["at"]} if "at" in old and "at" not in new else {}  # a request without a valid time keeps the last
    return {**new, **kept, "delta": delta}


async def _lock(conn: AsyncConnection[Any]) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))


async def add(conn: AsyncConnection[Any], env: Any, item: Mapping[str, Any]) -> None:
    await _lock(conn)
    session_id = item["session_id"]
    now = float(env.wall())
    cur = await conn.execute(
        "SELECT item, first_wall FROM runner.usage_pending WHERE session_id = %s FOR UPDATE", (session_id,)
    )
    row = await cur.fetchone()
    if row is not None and not mergeable(row[0], item):
        await env.outbox.enqueue(conn, "usage", row[0])  # another model, access or day: the pending item is complete
        row = None
    body, first = (dict(item), now) if row is None else (merged(row[0], item), float(row[1]))
    if now - first >= WINDOW_S:
        await env.outbox.enqueue(conn, "usage", body)
        await conn.execute("DELETE FROM runner.usage_pending WHERE session_id = %s", (session_id,))
        return
    await conn.execute(
        "INSERT INTO runner.usage_pending (session_id, item, first_wall) VALUES (%s, %s, %s)"
        " ON CONFLICT (session_id) DO UPDATE SET item = EXCLUDED.item, first_wall = EXCLUDED.first_wall",
        (session_id, Jsonb(body), first),
    )


async def flush(conn: AsyncConnection[Any], env: Any, every: bool = False) -> int:
    """Write the due rows as outbox items (module docstring); returns their number."""
    await _lock(conn)
    cur = await conn.execute(
        "SELECT p.session_id::text, p.item FROM runner.usage_pending p"
        " LEFT JOIN runner.session s ON s.id = p.session_id"
        " WHERE %s OR p.first_wall <= %s OR s.id IS NULL OR s.state = ANY(%s)"
        " ORDER BY p.first_wall, p.session_id FOR UPDATE OF p",
        (every, float(env.wall()) - WINDOW_S, sorted(FINAL)),
    )
    rows = await cur.fetchall()
    for session_id, body in rows:
        await env.outbox.enqueue(conn, "usage", body)
        await conn.execute("DELETE FROM runner.usage_pending WHERE session_id = %s", (session_id,))
    return len(rows)


async def tick(context: Any) -> None:
    service = running_service(context)
    if service is None:
        return
    async with service.env.transaction() as conn:
        await flush(conn, service.env)


async def run(context: Any) -> None:
    while not context.stopping.is_set():
        try:
            await tick(context)
        except Exception as exc:
            context.log.write(f"runner.model usage_merge_failed error={type(exc).__name__}\n")
            context.log.flush()
        await context.host.sleep(TICK_S)
