"""The platform budget a provisioning carries, with the cost the hub has not counted yet (KOR-10, AUD-02 NA-10;
03-api.md section 6.1 `platform_budget`, contract `guest/session-credential.request.json` `budget`).

`platform_budget.spent_today_by_org` and `last_origin_seq` of `GET /v1/device/state` come from one repeatable-read
snapshot, and the hub adds the cost of an outbox item `usage` of platform work to that amount in the transaction that
stores the item: the amount contains exactly this device's platform `usage` items up to `last_origin_seq`.
`budget(...)` therefore adds the cost (`delta.cost_micro_usd`) of the stored `usage` items above it that the hub will
count for the organization - the hub's rule `platform_work`: a session with `model_access` `platform`, or without a
known access and with `cost_basis` `provider` - and names the guest journal position the runner had taken into its
outbox (`journal_through`, the cursor of the session's VM; read after the items, so a page committed in between is in
neither part rather than in both). The guest gateway carries the cost of its own requests above that position across
the renewal; the parts never overlap.

The amount is added in integer micro-USD and turned into USD the way the hub does (`Decimal(micro) / 10**6`), so an
item that moves from "not counted" to "counted" leaves the same number. Within one hub day (UTC day of `hub_time`) the
amount sent for an organization never falls (`Marks`, per daemon): items the hub rejects, a stale desired state whose
items were pruned meanwhile or an unreadable row would otherwise lower it, and the gateway takes a lower amount for a
new day. The highest amount of the day is still a lower bound of the day's spending.

Without a known `last_origin_seq`, without the storage key, or for a session without VM nothing is added (but the
day's highest amount still applies) and no position is named (the gateway then keeps only its `old spent + observed`
bound). At most `MAX_ITEMS` items are read and a row that does not open is skipped (a lower bound still); only the
session ID, the cost basis and the cost of the opened bodies are used. A store that cannot be read leaves the hub's
amount. The opening runs on the event loop (one AEAD open per uncounted item; the items above the hub's position are
few while the hub is reachable).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg

from agent_runner.crypto.errors import CryptoError
from agent_runner.orgs.db import session as store_session
from agent_runner.sync import outbox

MAX_ITEMS = 10_000
MICRO = Decimal(1_000_000)
PLATFORM = "platform"
PROVIDER_COST = "provider"
ATTRIBUTE = "model_budget_marks"


@dataclass
class Marks:
    """Organization ID -> (hub day, highest amount in micro-USD sent that day), per daemon."""

    highest: dict[str, tuple[str, int]] = field(default_factory=dict)

    def keep(self, org_id: str, day: str | None, micro: int) -> int:
        """`micro`, or the day's higher amount sent before; a new day (or an unknown one) starts over."""
        if day is None:
            return micro
        seen = self.highest.get(org_id)
        if seen is not None and seen[0] == day:
            micro = max(micro, seen[1])
        self.highest[org_id] = (day, micro)
        return micro


def marks(context: Any) -> Marks:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Marks):
        return found
    created = Marks()
    if context is not None:
        setattr(context, ATTRIBUTE, created)
    return created


def _position(desired: Mapping[str, Any] | None) -> int | None:
    value = None if desired is None else desired.get("last_origin_seq")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _day(desired: Mapping[str, Any] | None) -> str | None:
    value = None if desired is None else desired.get("hub_time")
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def micro_of(usd: float) -> int:
    """USD of a JSON number as integer micro-USD (the hub sends `Decimal(micro) / 10**6` as a float)."""
    return int((Decimal(repr(usd)) * MICRO).to_integral_value())


def usd_of(micro: int) -> float:
    return float(Decimal(micro) / MICRO)


async def uncounted_micro(conn: Any, storage_key: bytes, org_id: str, after: int) -> int:
    """Micro-USD of the stored `usage` items above `after` that the hub will count for the organization."""
    cur = await conn.execute(
        "SELECT id::text, model_access FROM runner.session WHERE org_id = %s",
        (org_id,),
    )
    access = {str(sid): kind for sid, kind in await cur.fetchall()}
    cur = await conn.execute(
        "SELECT origin_seq, body_ct FROM runner.outbox WHERE item_kind = 'usage' AND origin_seq > %s"
        " ORDER BY origin_seq LIMIT %s",
        (after, MAX_ITEMS),
    )
    total = 0
    for seq, blob in await cur.fetchall():
        try:
            body = outbox.open_item(storage_key, seq, "usage", bytes(blob))
        except CryptoError:
            continue
        sid = body.get("session_id")
        if sid not in access:
            continue
        kind = access[sid]
        if kind == PLATFORM or (kind is None and body.get("cost_basis") == PROVIDER_COST):
            total += int(body["delta"]["cost_micro_usd"])
    return total


async def budget(
    env: Any, desired: Mapping[str, Any] | None, base: Mapping[str, float] | None, org_id: str, vm_id: str | None
) -> dict[str, Any] | None:
    """`base` (`provision.platform_budget(desired, ...)`) with the uncounted cost added and `journal_through` (module
    docstring); `desired` is the one snapshot of the desired state both come from. A store that cannot be read or a
    body outside the contract leaves the hub's amount (logged): the budget never fails a start or a renewal."""
    if base is None:
        return None
    spent = micro_of(base["spent_usd"])
    found: dict[str, Any] = dict(base)
    after = _position(desired)
    key = getattr(env.outbox, "storage_key", None)
    if after is not None and isinstance(key, bytes):
        try:
            async with store_session(env.db) as conn:
                extra = await uncounted_micro(conn, key, org_id, after)
                through = None
                if vm_id is not None:
                    cur = await conn.execute("SELECT journal_seq FROM runner.guest_cursor WHERE vm_id = %s", (vm_id,))
                    row = await cur.fetchone()
                    through = 0 if row is None else int(row[0])
        except (psycopg.Error, KeyError, TypeError, ValueError) as exc:
            env.log(f"runner.model uncounted_unavailable org_id={org_id} error={type(exc).__name__}")
        else:
            spent += extra
            if through is not None:
                found["journal_through"] = through
    if base["spent_usd"] < base["usd_per_day"]:  # a malformed budget fails closed at the limit: kept as it is
        found["spent_usd"] = usd_of(marks(env.context).keep(org_id, _day(desired), spent))
    return found
