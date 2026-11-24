"""Reconcile hints to devices after a membership-log change (03-api.md section 6.2, 05-runner.md section 2.2).

Assignments, removals and every other change of a device's desired state that comes from a membership log reach the
device as the hint `reconcile {}` (the runner then fetches `GET /v1/device/state`). The orgs area emits the signal
`log.appended` (`org_id`, `seq` = the new head) after every committed append; the subscriber sends the hint, through
`agent_hub.stream.publish` only, to

- every active device member of the org after the append, and
- every device that the appended batch names (`accept`, `device_assign`, `device_unassign`, `remove` of a device): a
  device that was just unassigned or removed must stop its sessions for that org.

The batch is the run of entries that share the receive time of the head entry (one append stores all its entries with
one time; an earlier batch with the very same time only adds a superfluous hint). A hint carries no content.

Signals carry identifiers only, and a subscriber gets no app context; the subscriber reads with the pool of the hub
context bound by the job `device.bind_context` (`agent_hub.device.jobs`, run once when the process starts its jobs)
and by the device resolver (every device request). Without a bound context it sends nothing; the `log` hint of the
orgs area still reaches every active device of the org, and every runner reconciles at least every 30 s
(05-runner.md section 2.2). The emitter calls subscribers on the request's event loop, so the subscriber only queues
the work on one background thread (`wait` lets tests and shutdown drain it; finished work is dropped at the next
append, so the list never grows in a long-running hub, KOR-06 A-13); a failure there is logged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import UUID

from agent_hub import signals
from agent_hub.db.pool import transaction
from agent_hub.stream.publish import Audience, publish

LOG = logging.getLogger("agent_hub.device")
_context: dict[str, Any] = {}
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="device-reconcile")
_pending: list[Future[None]] = []


def bind(context: Any) -> None:
    """Remember the hub context whose pool the subscriber reads (called by the device resolver)."""
    _context["hub"] = context


def devices_to_reconcile(connection: Any, org_id: UUID, seq: int) -> list[UUID]:
    """The active device members of the org and the devices named by the batch that ends at `seq`."""
    rows = connection.execute(
        "SELECT principal_id FROM hub.org_member WHERE org_id = %s AND kind = 'device' AND removed_seq IS NULL"
        " UNION SELECT e.subject_id FROM hub.membership_entry e JOIN hub.device d ON d.id = e.subject_id"
        " WHERE e.org_id = %s AND e.seq <= %s AND e.received_at ="
        " (SELECT received_at FROM hub.membership_entry WHERE org_id = %s AND seq = %s)",
        (org_id, org_id, seq, org_id, seq),
    ).fetchall()
    return sorted(UUID(str(row[0])) for row in rows)


def reconcile(context: Any, org_id: UUID, seq: int) -> None:
    """Send `reconcile {}` to the devices of the change (module docstring)."""
    if context.pool.closed:
        return
    try:
        with transaction(context.pool) as connection:
            devices = devices_to_reconcile(connection, org_id, seq)
        for device in devices:
            publish(Audience("device", device), "reconcile", {})
    except Exception:
        LOG.error("reconcile hints of a log append could not be sent", exc_info=True)


def on_log_appended(_name: str, ids: Mapping[str, Any]) -> None:
    """Queue the reconcile hints of a committed append (never blocks the emitter)."""
    context = _context.get("hub")
    if context is None:
        return
    _pending[:] = [future for future in _pending if not future.done()]  # keep only the queued and running ones
    _pending.append(_worker.submit(reconcile, context, ids["org_id"], ids["seq"]))


def wait() -> None:
    """Wait until every queued reconcile has run."""
    while _pending:
        _pending.pop(0).result()


SUBSCRIBERS: Mapping[str, Sequence[signals.Subscriber]] = {"log.appended": [on_log_appended]}
