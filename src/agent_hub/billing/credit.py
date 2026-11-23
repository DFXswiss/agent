"""The prepaid credit of an organization (12-billing.md sections 3 and 7, ADR-059 section 5).

`hub.credit_account` holds two numbers per organization: `balance_micro_usd` (what the organization has, settled)
and `booked_micro_usd` (what the platform's own bookings say it has). A push never locks the account: a charge only
inserts a `hub.charge_pending` row (`pending`). `settle` - the job `billing.settle`, the operator commands - locks
the account, deletes the org's pending rows and subtracts their sum from **both** columns; operator grants and sets
also move both. A balance set by hand in the database changes only `balance_micro_usd`; `settle` finds the
difference, records it as a `manual` credit entry with an audit row and sets `booked = balance`, in the same
transaction. A hand-set value is therefore never lost, never doubled and always visible in the history.

- **Balance** of an org: `balance_micro_usd - Σ pending`; an org without an account counts with the start credit.
- **Billing active** at time t: a price list version is in force (`prices.in_force`). Without one nothing is charged
  and nothing is blocked; accounts are created only while billing is active, with the start credit as configured at
  that moment (`AGENT_HUB_BILLING_START_CREDIT_USD`, history `start`).
- **Exhausted**: billing active and balance <= 0.
- Every history entry keeps its CHF equivalent at the rate of its day (`agent_hub.billing.fx`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.billing import binding, fx, prices

Connection = psycopg.Connection[Any]
MICRO = Decimal(1_000_000)
MAX_MICRO = 10**15
"""The largest amount the operator may grant or set: one billion USD."""
MAX_REASON = 500
_BALANCES = (
    "SELECT a.org_id::text, a.balance_micro_usd - COALESCE((SELECT sum(p.amount_micro_usd) FROM hub.charge_pending p"
    " WHERE p.org_id = a.org_id), 0) FROM hub.credit_account a WHERE a.org_id = ANY(%s::uuid[])"
)


class CreditInvalid(ValueError):
    """An operator credit that cannot be booked (amount out of range, reason missing)."""


@dataclass(frozen=True)
class Account:
    org_id: UUID
    balance_micro_usd: int
    booked_micro_usd: int


def micro(usd: Decimal) -> int:
    """USD as micro-USD (the callers accept at most 6 decimals)."""
    return int(usd * MICRO)


def start_micro() -> int:
    return micro(binding.start_credit())


def active(connection: Connection, now: datetime) -> bool:
    """A price list is in force at `now`."""
    return prices.in_force(connection, now) is not None


def balances(connection: Connection, org_ids: Iterable[UUID | str]) -> dict[str, int]:
    """The balance of each org (module docstring); the start credit for an org without an account."""
    wanted = sorted({str(org) for org in org_ids})
    found = {str(org): int(value) for org, value in connection.execute(_BALANCES, (wanted,)).fetchall()}
    start = start_micro()
    return {org: found.get(org, start) for org in wanted}


def balance(connection: Connection, org_id: UUID | str) -> int:
    return balances(connection, [org_id])[str(org_id)]


def exhausted_orgs(connection: Connection, org_ids: Iterable[UUID | str], now: datetime) -> dict[str, bool]:
    """`exhausted` of several orgs (module docstring)."""
    wanted = list(org_ids)
    if not active(connection, now):
        return {str(org): False for org in wanted}
    return {org: value <= 0 for org, value in balances(connection, wanted).items()}


def exhausted(connection: Connection, org_id: UUID | str, now: datetime) -> bool:
    return exhausted_orgs(connection, [org_id], now)[str(org_id)]


def _entry(
    connection: Connection,
    org_id: UUID | str,
    now: datetime,
    *,
    kind: str,
    amount: int,
    balance_after: int,
    reason: str | None,
    actor: str,
) -> None:
    connection.execute(
        "INSERT INTO hub.credit_entry (org_id, n, at, kind, amount_micro_usd, balance_micro_usd, reason, actor,"
        " fx_n, fx_day, chf_per_usd_e8, amount_micro_chf) SELECT %s, COALESCE(max(n), 0) + 1, %s, %s, %s, %s, %s, %s,"
        " %s, %s, %s, %s FROM hub.credit_entry WHERE org_id = %s",
        (org_id, now, kind, amount, balance_after, reason, actor, *fx.equivalent(connection, now, amount), org_id),
    )
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="operator" if actor == "operator" else "hub",
            action="credit.changed",
            result="ok",
            org_id=UUID(str(org_id)),
            target_kind="org",
            target_id=str(org_id),
            detail={"kind": kind, "amount_micro_usd": amount, "balance_micro_usd": balance_after, "actor": actor},
        ),
    )


def ensure(connection: Connection, org_id: UUID | str, now: datetime) -> bool:
    """Create the org's account with the start credit when it has none; True when it was created."""
    start = start_micro()
    created = connection.execute(
        "INSERT INTO hub.credit_account (org_id, balance_micro_usd, booked_micro_usd, updated_at)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT (org_id) DO NOTHING RETURNING org_id",
        (org_id, start, start, now),
    ).fetchone()
    if created is None:
        return False
    _entry(connection, org_id, now, kind="start", amount=start, balance_after=start, reason=None, actor="hub")
    return True


def pend(connection: Connection, charge_id: UUID | str, org_id: UUID | str, amount: int) -> None:
    """A booked charge waiting for the settle (never locks the account)."""
    connection.execute(
        "INSERT INTO hub.charge_pending (charge_id, org_id, amount_micro_usd) VALUES (%s, %s, %s)",
        (charge_id, org_id, amount),
    )


def settle(connection: Connection, org_id: UUID | str, now: datetime) -> Account:
    """Lock the org's account (created when missing), take the pending charges from it and record a hand-set
    difference as a `manual` entry (module docstring); the settled account."""
    ensure(connection, org_id, now)
    row = cast(
        tuple[Any, int, int],
        connection.execute(
            "SELECT org_id, balance_micro_usd, booked_micro_usd FROM hub.credit_account WHERE org_id = %s FOR UPDATE",
            (org_id,),
        ).fetchone(),
    )
    current, booked = int(row[1]), int(row[2])
    if current != booked:
        _entry(
            connection,
            org_id,
            now,
            kind="manual",
            amount=current - booked,
            balance_after=current,
            reason=None,
            actor="database",
        )
    taken = cast(
        tuple[int],
        connection.execute(
            "WITH gone AS (DELETE FROM hub.charge_pending WHERE org_id = %s RETURNING amount_micro_usd)"
            " SELECT COALESCE(sum(amount_micro_usd), 0) FROM gone",
            (org_id,),
        ).fetchone(),
    )
    settled = current - int(taken[0])
    connection.execute(
        "UPDATE hub.credit_account SET balance_micro_usd = %s, booked_micro_usd = %s, updated_at = %s"
        " WHERE org_id = %s",
        (settled, settled, now, org_id),
    )
    return Account(UUID(str(row[0])), settled, settled)


def _reason(reason: str) -> str:
    text = reason.strip()
    if not text or len(text) > MAX_REASON:
        raise CreditInvalid(f"the reason must be a text of 1 to {MAX_REASON} characters")
    return text


def _amount(usd: Decimal) -> int:
    amount = micro(usd)
    if abs(amount) > MAX_MICRO:
        raise CreditInvalid("the amount is out of range")
    return amount


def grant(connection: Connection, org_id: UUID | str, usd: Decimal, reason: str, now: datetime) -> Account:
    """The operator adds `usd` (negative to correct) to the org's balance."""
    amount, text = _amount(usd), _reason(reason)
    settled = settle(connection, org_id, now)
    after = settled.balance_micro_usd + amount
    connection.execute(
        "UPDATE hub.credit_account SET balance_micro_usd = %s, booked_micro_usd = %s, updated_at = %s"
        " WHERE org_id = %s",
        (after, after, now, org_id),
    )
    _entry(connection, org_id, now, kind="grant", amount=amount, balance_after=after, reason=text, actor="operator")
    return Account(settled.org_id, after, after)


def set_balance(connection: Connection, org_id: UUID | str, usd: Decimal, reason: str, now: datetime) -> Account:
    """The operator sets the org's balance to `usd`."""
    target, text = _amount(usd), _reason(reason)
    settled = settle(connection, org_id, now)
    connection.execute(
        "UPDATE hub.credit_account SET balance_micro_usd = %s, booked_micro_usd = %s, updated_at = %s"
        " WHERE org_id = %s",
        (target, target, now, org_id),
    )
    _entry(
        connection,
        org_id,
        now,
        kind="set",
        amount=target - settled.balance_micro_usd,
        balance_after=target,
        reason=text,
        actor="operator",
    )
    return Account(settled.org_id, target, target)


def due(connection: Connection, now: datetime) -> list[str]:
    """The orgs the settle job must visit: hand-set balances always; while billing is active also pending charges and
    orgs without an account (charges are only booked while it is active, so pending rows outside it are only those of
    a restored copy, which a restored hub must not change)."""
    found = {
        str(org)
        for (org,) in connection.execute(
            "SELECT org_id FROM hub.credit_account WHERE balance_micro_usd <> booked_micro_usd"
        ).fetchall()
    }
    if active(connection, now):
        found.update(
            str(org)
            for (org,) in connection.execute(
                "SELECT DISTINCT org_id FROM hub.charge_pending UNION SELECT o.id FROM hub.org o"
                " WHERE NOT EXISTS (SELECT 1 FROM hub.credit_account a WHERE a.org_id = o.id)"
            ).fetchall()
        )
    return sorted(found)
