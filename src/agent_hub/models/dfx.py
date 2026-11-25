"""Per-device DFX keys: DFX model access on own hardware (ADR-081 section 7, KOR-50; 12-billing.md case 3).

A platform device whose runner holds a management key of the model provider announces itself (`dfx_key_state
{issuer: true}`, `hub.dfx_issuer`). The **issuer** is the online, active platform device with the smallest ID among
them (`issuer`); only it gets `dfx_keys` in its device state.

**Requests** (`plan`, the job `models.dfx_keys`): while billing is active, every active org device that is an active
member of an organization whose credit is not exhausted gets one request row in `hub.dfx_key` (one row per org device
and organization that is not revoked, `dfx_key_live`). A row whose org device left the organization, was removed or
decertified, or is disabled, is revoked: its envelope is dropped at once. The issuer's state lists the open requests
that have headroom (`requests`, with the org device's KEM public key from the hub's device registry and the
`headroom_usd` of its org's keys, the limit of the new key; no headroom - billing not active, the org exhausted - no
request is listed) and every issued key that is not closed (`keys`): `headroom_usd` is the org's balance divided by
the `platform` factor of the price list in force and evenly over the org's live keys, at most
`AGENT_HUB_DFX_KEY_HEADROOM_USD`; `disabled` while the org is exhausted or billing is not active; `revoke` for a
revoked key (then `disabled` and no headroom).

**Issued** (`issued`, outbox `dfx_key_issued`, only from a platform device): the request is found by its ID and must
name the same org and device (`request_invalid`); an unknown request is `404 not_found`; a second issue with another
key reference, or a key reference of another request, is `409 id_conflict`; the same issue again changes nothing. The
hub stores the key reference, the issuer and the envelope (opaque ciphertext; dropped for a revoked request, whose key
is then listed for deletion) and hints the org device.

**Usage** (`usage_booked`, outbox `dfx_key_usage`, only from a platform device): the report is the provider's
cumulative spend. The hub books the difference to the highest figure it booked for the key (`booked_micro_usd`; a
lower or equal figure books nothing), so a repeated report, a new issuer or a crash never books twice. The provider
cost is split over the device's sessions by the provider cost their usage items reported since the last booking
(`hub.dfx_key_cost`, written by `record_cost`): each session gets at most its reported cost (largest remainder when the
provider reports less than the sessions did; the rest of their costs waits for the next booking), the share no session
reported stays with the organization and the device. The amount is the provider cost times the `platform` factor of
the price list in force at the report's time (rounded once, half-even) and is split in the same proportions (largest
remainder), one `tokens` charge per share (`agent_hub.billing.outbox.book`, quantity `{source: "provider_key",
key_ref, cost_micro_usd, device_id}`, the org device in the day's row). Without a price list nothing is booked, but
the figure advances. A report for a revoked key at or after its revocation is its final spend: the row is closed and
no longer listed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.billing import credit, prices
from agent_hub.billing.outbox import book
from agent_hub.crypto.encoding import b64u_encode, parse_time
from agent_hub.crypto.ids import uuid7
from agent_hub.errors import HubError
from agent_hub.outbox.base import ItemContext
from agent_hub.outbox.bodies import DfxKeyIssuedBody, DfxKeyUsageBody

Connection = psycopg.Connection[Any]
MICRO = Decimal(1_000_000)
PLATFORM = "platform"
SOURCE = "provider_key"
_SERVING = (
    "SELECT m.org_id, m.principal_id FROM hub.org_member m"
    " JOIN hub.device d ON d.id = m.principal_id AND d.kind = 'org' AND d.state = 'active'"
    " JOIN hub.principal p ON p.id = d.id AND p.disabled_at IS NULL"
    " WHERE m.kind = 'device' AND m.removed_seq IS NULL"
)
_REVOKE = (
    f"UPDATE hub.dfx_key k SET revoked_at = %s, envelope = NULL WHERE k.revoked_at IS NULL"
    f" AND (k.org_id, k.device_id) NOT IN ({_SERVING}) RETURNING k.device_id"
)
_REQUEST = (
    "INSERT INTO hub.dfx_key (request_id, org_id, device_id, requested_at) VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (org_id, device_id) WHERE revoked_at IS NULL DO NOTHING RETURNING request_id"
)


def issuer(connection: Connection, now: datetime, window: int) -> UUID | None:
    """The elected issuer (module docstring), None without one."""
    row = connection.execute(
        "SELECT d.id FROM hub.dfx_issuer i JOIN hub.device d ON d.id = i.device_id AND d.kind = 'platform'"
        " AND d.state = 'active' JOIN hub.principal p ON p.id = d.id AND p.disabled_at IS NULL"
        " JOIN hub.device_presence dp ON dp.device_id = d.id AND dp.last_heartbeat_at > %s"
        " ORDER BY d.id::text LIMIT 1",
        (now - timedelta(seconds=window),),
    ).fetchone()
    return None if row is None else UUID(str(row[0]))


@dataclass(frozen=True)
class Plan:
    """What one pass of `plan` changed: the new requests and the org devices whose key was revoked."""

    requested: tuple[UUID, ...]
    revoked: tuple[UUID, ...]


def plan(connection: Connection, now: datetime) -> Plan:
    """Revoke the keys of devices that left and request keys for the serving devices (module docstring)."""
    revoked = sorted({UUID(str(row[0])) for row in connection.execute(_REVOKE, (now,)).fetchall()})
    requested: list[UUID] = []
    if credit.active(connection, now):
        pairs = connection.execute(f"{_SERVING} ORDER BY m.org_id, m.principal_id").fetchall()
        exhausted = credit.exhausted_orgs(connection, sorted({str(org) for org, _device in pairs}), now)
        millis = int(now.timestamp() * 1000)
        for org, device in pairs:
            if exhausted[str(org)]:
                continue
            row = connection.execute(_REQUEST, (uuid7(millis), org, device, now)).fetchone()
            if row is not None:
                requested.append(UUID(str(row[0])))
    return Plan(tuple(requested), tuple(revoked))


@dataclass(frozen=True)
class Room:
    """The headroom of one organization's keys."""

    headroom_usd: float
    disabled: bool


def rooms(connection: Connection, orgs: Sequence[str], now: datetime, cap: Decimal) -> dict[str, Room]:
    """The headroom of the keys of each org (module docstring)."""
    version = prices.in_force(connection, now)
    if version is None:
        return {org: Room(0.0, True) for org in orgs}
    factor = version.prices.factors[PLATFORM]
    balances = credit.balances(connection, orgs)
    live = dict(
        connection.execute(
            "SELECT org_id::text, count(*) FROM hub.dfx_key WHERE org_id = ANY(%s::uuid[]) AND issued_at IS NOT NULL"
            " AND revoked_at IS NULL GROUP BY org_id",
            (list(orgs),),
        ).fetchall()
    )
    found: dict[str, Room] = {}
    for org in orgs:
        balance = balances[org]
        if balance <= 0:
            found[org] = Room(0.0, True)
            continue
        usd = Decimal(balance) / MICRO
        room = cap if factor == 0 else usd / factor / max(int(live.get(org, 0)), 1)
        found[org] = Room(float(min(room, cap).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)), False)
    return found


def issuer_state(connection: Connection, now: datetime, cap: Decimal) -> dict[str, list[dict[str, Any]]]:
    """`dfx_keys` of the elected issuer (module docstring)."""
    open_requests = connection.execute(
        "SELECT k.request_id, k.org_id, k.device_id, d.kem_pub FROM hub.dfx_key k"
        " JOIN hub.device d ON d.id = k.device_id WHERE k.issued_at IS NULL AND k.revoked_at IS NULL"
        " ORDER BY k.requested_at, k.request_id"
    ).fetchall()
    keys = connection.execute(
        "SELECT key_ref, org_id, device_id, revoked_at IS NOT NULL FROM hub.dfx_key"
        " WHERE issued_at IS NOT NULL AND closed_at IS NULL ORDER BY issued_at, key_ref"
    ).fetchall()
    orgs = sorted({str(row[1]) for row in [*open_requests, *keys]})
    room = rooms(connection, orgs, now, cap)
    requests = [
        {
            "request_id": str(request),
            "org_id": str(org),
            "device_id": str(device),
            "device_kem": b64u_encode(bytes(kem)),
            "headroom_usd": room[str(org)].headroom_usd,
        }
        for request, org, device, kem in open_requests
        if room[str(org)].headroom_usd > 0
    ]
    listed = []
    for key_ref, org, device, revoke in keys:
        found = room[str(org)]
        disabled = revoke or found.disabled
        listed.append(
            {
                "key_ref": key_ref,
                "org_id": str(org),
                "device_id": str(device),
                "headroom_usd": 0.0 if disabled else found.headroom_usd,
                "disabled": disabled,
                "revoke": revoke,
            }
        )
    return {"requests": requests, "keys": listed}


def held(connection: Connection, device: UUID) -> list[dict[str, str]]:
    """`dfx_key_held` of an org device: the envelope of each of its live keys."""
    rows = connection.execute(
        "SELECT org_id, key_ref, envelope, issuer_device_id FROM hub.dfx_key WHERE device_id = %s"
        " AND envelope IS NOT NULL AND revoked_at IS NULL ORDER BY org_id",
        (device,),
    ).fetchall()
    return [
        {"org_id": str(org), "key_ref": key_ref, "envelope": envelope, "issuer_device_id": str(issuer_device)}
        for org, key_ref, envelope, issuer_device in rows
    ]


def platform_device(connection: Connection, device: UUID) -> bool:
    row = connection.execute("SELECT kind FROM hub.device WHERE id = %s", (device,)).fetchone()
    return row is not None and row[0] == PLATFORM


def require_platform(item: ItemContext) -> None:
    """The items of this module come from platform devices only (`403 forbidden`)."""
    if not platform_device(item.connection, item.device):
        raise HubError("forbidden")


def _same_target(row: Sequence[Any], org_id: str, device_id: str) -> None:
    if (str(row[0]), str(row[1])) != (org_id, device_id):
        raise HubError("request_invalid")


def issued(item: ItemContext, body: DfxKeyIssuedBody) -> None:
    """Store an issued key (module docstring)."""
    require_platform(item)
    row = item.connection.execute(
        "SELECT org_id, device_id, key_ref, envelope, revoked_at FROM hub.dfx_key WHERE request_id = %s FOR UPDATE",
        (body.request_id,),
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    _same_target(row, body.org_id, body.device_id)
    other = item.connection.execute(
        "SELECT 1 FROM hub.dfx_key WHERE key_ref = %s AND request_id <> %s", (body.key_ref, body.request_id)
    ).fetchone()
    if other is not None or (row[2] is not None and row[2] != body.key_ref):
        raise HubError("id_conflict")
    if row[2] is not None:
        return
    envelope = None if row[4] is not None else body.envelope
    item.connection.execute(
        "UPDATE hub.dfx_key SET issuer_device_id = %s, key_ref = %s, envelope = %s, issued_at = %s"
        " WHERE request_id = %s",
        (item.device, body.key_ref, envelope, item.now, body.request_id),
    )
    if envelope is not None:
        item.effects.hint(row[1], "reconcile", {})


def split(total: int, weights: Sequence[tuple[Any, int]]) -> list[tuple[Any, int]]:
    """`total` split in proportion to the positive `weights` by largest remainder (ties: the earlier entry)."""
    whole = sum(weight for _key, weight in weights)
    shares = [(key, total * weight // whole, total * weight % whole) for key, weight in weights]
    left = total - sum(share for _key, share, _rest in shares)
    order = sorted(range(len(shares)), key=lambda index: (-shares[index][2], index))
    extra = set(order[:left])
    return [(key, share + (1 if index in extra else 0)) for index, (key, share, _rest) in enumerate(shares)]


def _allocate(connection: Connection, org: UUID, device: UUID, delta: int) -> list[tuple[Any, int]]:
    """The provider cost `delta` per session of the device (`None`: the org's share); consumes `hub.dfx_key_cost`."""
    costs = [
        (UUID(str(session)), int(cost))
        for session, cost in connection.execute(
            "SELECT c.session_id, c.cost_micro_usd FROM hub.dfx_key_cost c JOIN hub.session s ON s.id = c.session_id"
            " WHERE c.org_id = %s AND c.device_id = %s AND c.cost_micro_usd > 0 ORDER BY c.session_id FOR UPDATE",
            (org, device),
        ).fetchall()
    ]
    reported = sum(cost for _session, cost in costs)
    if reported <= delta:
        allocated = costs
        rest = delta - reported
    else:
        allocated = split(delta, costs)
        rest = 0
    for (session, cost), (_same, share) in zip(costs, allocated, strict=True):
        connection.execute(
            "UPDATE hub.dfx_key_cost SET cost_micro_usd = %s WHERE device_id = %s AND session_id = %s",
            (cost - share, device, session),
        )
    connection.execute("DELETE FROM hub.dfx_key_cost WHERE device_id = %s AND cost_micro_usd = 0", (device,))
    parts: list[tuple[Any, int]] = [(session, share) for session, share in allocated if share > 0]
    if rest > 0:
        parts.append((None, rest))
    return parts


def _book(item: ItemContext, body: DfxKeyUsageBody, delta: int, at: datetime) -> None:
    org, device = UUID(body.org_id), UUID(body.device_id)
    parts = _allocate(item.connection, org, device, delta)
    version = prices.in_force(item.connection, at)
    if version is None:
        return
    amount = int((Decimal(delta) * version.prices.factors[PLATFORM]).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    amounts = split(amount, parts)
    for index, ((session, cost), (_same, share)) in enumerate(zip(parts, amounts, strict=True)):
        ids: tuple[UUID, UUID | None, UUID | None, UUID | None] = (org, None, None, None)
        if session is not None:
            row = cast(
                tuple[UUID, UUID, UUID],
                item.connection.execute(
                    "SELECT wg_id, task_id, id FROM hub.session WHERE id = %s", (session,)
                ).fetchone(),
            )
            ids = (org, *row)
        quantity = {"source": SOURCE, "key_ref": body.key_ref, "cost_micro_usd": cost, "device_id": body.device_id}
        book(
            item,
            ids,
            kind="tokens",
            at=at,
            amount=share,
            version=version.version,
            quantity=quantity,
            count=0,
            part=index,
            daily_device=device,
        )


def usage_booked(item: ItemContext, body: DfxKeyUsageBody) -> None:
    """Book the spend of a key since its last booking (module docstring)."""
    require_platform(item)
    row = item.connection.execute(
        "SELECT org_id, device_id, booked_micro_usd, revoked_at, closed_at FROM hub.dfx_key WHERE key_ref = %s"
        " FOR UPDATE",
        (body.key_ref,),
    ).fetchone()
    if row is None:
        raise HubError("not_found")
    _same_target(row, body.org_id, body.device_id)
    if row[4] is not None:
        return
    at = min(parse_time(body.at), item.now)
    delta = body.spent_micro_usd - int(row[2])
    if delta > 0:
        _book(item, body, delta, at)
        item.connection.execute(
            "UPDATE hub.dfx_key SET booked_micro_usd = %s, booked_at = %s WHERE key_ref = %s",
            (body.spent_micro_usd, item.now, body.key_ref),
        )
    if row[3] is not None and at >= row[3]:
        item.connection.execute("UPDATE hub.dfx_key SET closed_at = %s WHERE key_ref = %s", (item.now, body.key_ref))


def record_cost(connection: Connection, org: UUID, device: UUID, session: UUID, cost: int) -> None:
    """Add a session's reported provider cost on the device's key (the weight of the next split)."""
    connection.execute(
        "INSERT INTO hub.dfx_key_cost (device_id, session_id, org_id, cost_micro_usd) SELECT %s, %s, %s, %s"
        " WHERE EXISTS (SELECT 1 FROM hub.dfx_key WHERE org_id = %s AND device_id = %s AND issued_at IS NOT NULL"
        " AND revoked_at IS NULL) ON CONFLICT (device_id, session_id)"
        " DO UPDATE SET cost_micro_usd = hub.dfx_key_cost.cost_micro_usd + EXCLUDED.cost_micro_usd",
        (device, session, org, cost, org, device),
    )
