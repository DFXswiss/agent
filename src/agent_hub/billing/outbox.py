"""Bookings from the outbox (12-billing.md sections 4 and 7, ADR-062 sections 1, 3 and 4; ADR-027 section 4
`<area>/outbox.py:HANDLERS`, ADR-030, ADR-037).

- `usage` (an extension after the usage area's base handler): a request that used **DFX model access** is charged by
  the price list in force at the request time (`at`, clamped to the receive time and to the session's creation, so a
  backdated time cannot pick an older or no price list; the receive time without one). The
  origin is the item's `access_origin` (AUD-04 W10): `platform` (the platform key) and `operator` (an operator
  subscription, ADR-052) are charged with the factor of that name, `own` is free. An older runner omits it: then the
  session's `model_access = platform` (or an unknown access with the provider cost basis, as
  `agent_hub.usage.outbox.platform_work`) counts as `platform`, anything else as `own` - operator subscriptions are
  charged once the runner is updated. The model is the item's `model`, else the session's.
- Tokens are charged only for items of a **platform device** (ADR-062 section 1: the hub decides, an org device can
  neither cause nor avoid a charge).
- `runtime` (base handler claimed here): VM runtime attributed to a session of an org the pushing device serves (also
  a session that moved to another device since, so late items are not lost). Only a platform device is charged; the
  item of an org device is acknowledged and ignored. `runtime_ms` may not exceed the interval `to - from`, and `to`
  may not lie after the receive time by more than the clock skew allowance.

Each charge is one row of `hub.charge` (unique per device and `origin_seq`: an outbox replay runs no handler, and the
key refuses a second row should one ever run twice), added to `hub.charge_daily` (org, UTC day of the measurement,
workgroup, device, kind: amount and quantity; with its CHF equivalent at the rate of its day, `agent_hub.billing.fx`)
and left in `hub.charge_pending` for the settle job, which takes it from
the org's credit (`agent_hub.billing.credit`); the push never locks the org's account.
Without a price list in force nothing is booked.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from agent_hub.billing import credit, fx, prices
from agent_hub.crypto.encoding import parse_time
from agent_hub.crypto.ids import uuid7
from agent_hub.errors import HubError
from agent_hub.outbox.base import Handler, ItemContext, session_on_device
from agent_hub.outbox.bodies import RuntimeBody, UsageBody
from agent_hub.usage.budget import utc_day
from agent_hub.usage.outbox import platform_work

LOG = logging.getLogger("agent_hub.billing")
SKEW = timedelta(seconds=300)
"""How far a measured time may lie after the hub's receive time (device clock skew)."""
COUNTS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
INSERT_CHARGE = (
    "INSERT INTO hub.charge (id, org_id, wg_id, task_id, session_id, device_id, origin_seq, kind, at,"
    " amount_micro_usd, price_version, quantity, fx_n, fx_day, chf_per_usd_e8, amount_micro_chf)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (device_id, origin_seq) DO NOTHING RETURNING id"
)
UPSERT_DAILY = (
    "INSERT INTO hub.charge_daily (org_id, day, wg_id, device_id, kind, amount_micro_usd, quantity)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (org_id, day, wg_id, device_id, kind) DO UPDATE"
    " SET amount_micro_usd = hub.charge_daily.amount_micro_usd + EXCLUDED.amount_micro_usd,"
    " quantity = hub.charge_daily.quantity + EXCLUDED.quantity"
)


def served_session(item: ItemContext, session_id: str) -> tuple[UUID, UUID, UUID, UUID]:
    """`(org_id, wg_id, task_id, session_id)` of a session of an org the device serves (`404`/`403` otherwise)."""
    row = item.connection.execute(
        "SELECT org_id, wg_id, task_id, id FROM hub.session WHERE id = %s", (session_id,)
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    if str(row[0]) not in {org for org, _wg in item.view().assigned()}:
        raise HubError("forbidden")
    return row[0], row[1], row[2], row[3]


def measured(item: ItemContext, text: str | None) -> datetime:
    """The measurement time: the reported one, never after the receive time; the receive time without one."""
    if text is None:
        return item.now
    return min(parse_time(text), item.now)


def book(
    item: ItemContext,
    ids: tuple[UUID, UUID, UUID, UUID],
    *,
    kind: str,
    at: datetime,
    amount: int,
    version: int,
    quantity: Mapping[str, Any],
    count: int,
) -> None:
    """Insert the charge, add it to the day and leave it pending for the credit (module docstring)."""
    org, wg, task, session = ids
    inserted = item.connection.execute(
        INSERT_CHARGE,
        (
            uuid7(int(at.timestamp() * 1000)),
            org,
            wg,
            task,
            session,
            item.device,
            item.origin_seq,
            kind,
            at,
            amount,
            version,
            Jsonb(dict(quantity)),
            *fx.equivalent(item.connection, at, amount),
        ),
    ).fetchone()
    if inserted is None:
        return
    item.connection.execute(UPSERT_DAILY, (org, utc_day(at), wg, item.device, kind, amount, count))
    credit.pend(item.connection, inserted[0], org, amount)


def platform_device(item: ItemContext) -> bool:
    row = item.connection.execute("SELECT kind FROM hub.device WHERE id = %s", (item.device,)).fetchone()
    return row is not None and row[0] == "platform"


def book_tokens(item: ItemContext, body: UsageBody) -> None:
    """The token charge of one `usage` item (module docstring)."""
    ids = session_on_device(item, body.session_id)
    if not platform_device(item):
        return
    row = cast(
        tuple[str | None, str | None, datetime],
        item.connection.execute(
            "SELECT model_access, model_id, created_at FROM hub.session WHERE id = %s", (ids[3],)
        ).fetchone(),
    )
    access, session_model, created = row
    origin = body.access_origin or ("platform" if platform_work(access, body.cost_basis) else "own")
    if origin == "own":
        return
    at = max(measured(item, body.at), created)  # a request is never older than its session
    version = prices.in_force(item.connection, at)
    if version is None:
        return
    kind = origin
    model = body.model or session_model
    counts = {name: getattr(body.delta, name) for name in COUNTS}
    amount = prices.token_charge(version.prices, kind, model, counts)
    quantity = {**counts, "model": model, "access": kind}
    book(
        item,
        ids,
        kind="tokens",
        at=at,
        amount=amount,
        version=version.version,
        quantity=quantity,
        count=sum(counts.values()),
    )


def book_runtime(item: ItemContext, body: RuntimeBody) -> None:
    """The runtime charge of one `runtime` item (module docstring)."""
    ids = served_session(item, body.session_id)
    start, end = parse_time(body.from_), parse_time(body.to)
    if body.runtime_ms > (end - start) / timedelta(milliseconds=1) or end > item.now + SKEW:
        raise HubError("request_invalid")
    if not platform_device(item):
        LOG.info("billing.runtime ignored device_id=%s reason=org_device", item.device)
        return
    at = min(end, item.now)
    version = prices.in_force(item.connection, at)
    if version is None:
        return
    size = "session" if body.vm_kind == "session" else body.size
    amount = prices.runtime_charge(version.prices, size, body.runtime_ms)
    quantity = {"runtime_ms": body.runtime_ms, "vm_kind": body.vm_kind, "size": size, "vm_id": body.vm_id}
    book(
        item,
        ids,
        kind="runtime",
        at=at,
        amount=amount,
        version=version.version,
        quantity=quantity,
        count=body.runtime_ms,
    )


HANDLERS: Mapping[str, Handler] = {"usage": book_tokens, "runtime": book_runtime}
BASE_KINDS = frozenset({"runtime"})
"""This area's `runtime` handler is the base handler of the kind (`agent_hub.outbox.chains`, ADR-037)."""
