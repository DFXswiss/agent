"""The settle job of the billing area (ADR-027 section 4 `<area>/jobs.py:JOBS`, ADR-059 sections 5 and 6).

`billing.settle` runs at start (`at_start`: it binds the configuration before the first request) and every 5 s:

1. binds the start credit (`agent_hub.billing.binding`);
2. settles every due org in its own short transaction (`credit.due`, `credit.settle`): missing accounts while billing
   is active, pending charges, balances set by hand in the database;
3. compares every org's `exhausted` with the previous pass of this process. An org whose state changed - a charge took
   the balance to 0, a credit, a hand-set balance, a price list coming into force - gets the signal `credit.changed`
   (the scheduler places its waiting sessions) and the content-free hint `reconcile {}` to the devices assigned to it,
   so runners stop or continue within seconds. The first pass only records the states (a starting hub has nothing to
   announce; runners fetch the device state on connect);
4. warns in the log once an hour while billing is active and the USD/CHF rate is missing or older than
   `fx.STALE_DAYS` days (`agent_hub.billing.fx`; bookings go on with the newest rate).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from agent_hub import signals
from agent_hub.billing import binding, credit, fx
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job
from agent_hub.stream.publish import hint

SETTLE_INTERVAL_SECONDS = 5.0
LOG = logging.getLogger("agent_hub.billing")
_known: dict[str, bool] = {}
_warned: dict[str, Any] = {}
STALE_WARNING_INTERVAL = timedelta(hours=1)


def forget() -> None:
    """Forget the states of the previous pass (tests)."""
    _known.clear()
    _warned.clear()


def states(connection: Any, now: Any) -> dict[str, bool]:
    """`exhausted` of every org."""
    orgs = [str(row[0]) for row in connection.execute("SELECT id FROM hub.org ORDER BY id").fetchall()]
    return credit.exhausted_orgs(connection, orgs, now)


def devices_of(connection: Any, org_id: str) -> list[UUID]:
    rows = connection.execute(
        "SELECT DISTINCT a.device_id FROM hub.wg_device a JOIN hub.wg w ON w.id = a.wg_id"
        " WHERE w.org_id = %s AND a.unassigned_seq IS NULL ORDER BY a.device_id",
        (org_id,),
    ).fetchall()
    return [UUID(str(row[0])) for row in rows]


def warn_stale(connection: Any, now: Any) -> bool:
    """While billing is active, warn once an hour when the USD/CHF rate is missing or older than `fx.STALE_DAYS`."""
    if not credit.active(connection, now):
        return False
    age = fx.age_days(connection, now)
    if age is not None and age <= fx.STALE_DAYS:
        return False
    last = _warned.get("fx")
    if last is not None and now - last < STALE_WARNING_INTERVAL:
        return False
    _warned["fx"] = now
    LOG.warning("billing.fx stale age_days=%s (set the USD/CHF rate: agent-hub billing fx set)", age)
    return True


def settle_pass(context: Any) -> list[str]:
    """One pass (module docstring); the orgs whose `exhausted` changed."""
    binding.bind(context.config)
    with transaction(context.pool) as connection:
        due = credit.due(connection, context.clock())
    for org in due:
        with transaction(context.pool) as connection:
            credit.settle(connection, org, context.clock())
    with transaction(context.pool) as connection:
        current = states(connection, context.clock())
        first = not _known
        changed = [org for org, value in current.items() if not first and _known.get(org, False) != value]
        devices = {org: devices_of(connection, org) for org in changed}
        warn_stale(connection, context.clock())
    _known.clear()
    _known.update(current)
    _known.setdefault("", False)  # marks that a pass ran, also without any org
    for org in changed:
        LOG.info("billing.credit org_id=%s exhausted=%s", org, "yes" if current[org] else "no")
        signals.emit("credit.changed", org_id=UUID(org))
        for device in devices[org]:
            hint(device, "reconcile")
    return changed


JOBS = (Job("billing.settle", SETTLE_INTERVAL_SECONDS, settle_pass, at_start=True),)
