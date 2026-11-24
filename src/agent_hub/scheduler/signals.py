"""Re-scheduling on presence, capacity and assignment signals (03-api.md section 4.6.2, ADR-027 section 4).

Subscribers of `device.presence` (`device_id`, `online`) and `device.capacity` (`device_id`) from the stream area
(HUB-07) and of `wraps.stored` (`device_id`, a key reached a device: ADR-060 `keys_pending`) re-run the scheduler
for the waiting sessions of the device's workgroups; `log.appended` (`org_id`, `seq`) from the orgs area does it for
the org's workgroups (a `device_assign` makes a device a candidate). Signals carry
identifiers only and no app context: like the reconcile hints of the device area, the subscribers read with the pool
of a bound hub context (bound by the tasks routes and by the job `scheduler.reschedule`, `agent_hub.scheduler.jobs`)
and send nothing before one is bound - the job's sweep places those sessions at its next run. A subscriber only queues
the work on one background thread (the emitter may run on the event loop); failures there are logged. `wait` drains
the queue (tests, shutdown).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from agent_hub import signals
from agent_hub.db.pool import transaction
from agent_hub.scheduler.reschedule import BY_DEVICE, BY_ORG, reschedule, waiting_wgs

LOG = logging.getLogger("agent_hub.scheduler")
_context: dict[str, Any] = {}
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scheduler")
_pending: list[Future[None]] = []


def bind(context: Any) -> None:
    """Remember the hub context whose pool the subscribers use."""
    _context["hub"] = context


def run(where: str, params: tuple[Any, ...]) -> None:
    """Re-schedule the waiting sessions of the restricted workgroups (logged on failure)."""
    context = _context.get("hub")
    if context is None or context.pool.closed:
        return
    try:
        with transaction(context.pool) as connection:
            wgs = waiting_wgs(connection, context.clock(), where, params)
        reschedule(context, wgs)
    except Exception:
        LOG.error("waiting sessions could not be re-scheduled", exc_info=True)


def _queue(where: str, params: tuple[Any, ...]) -> None:
    if _context.get("hub") is not None:
        _pending[:] = [future for future in _pending if not future.done()]
        _pending.append(_worker.submit(run, where, params))


def on_device(_name: str, ids: Mapping[str, Any]) -> None:
    """`device.presence` / `device.capacity`: the device's workgroups."""
    _queue(BY_DEVICE, (ids["device_id"],))


def on_log(_name: str, ids: Mapping[str, Any]) -> None:
    """`log.appended`: the org's workgroups."""
    _queue(BY_ORG, (ids["org_id"],))


def wait() -> None:
    """Wait until every queued re-scheduling has run."""
    while _pending:
        _pending.pop(0).result()


SUBSCRIBERS: Mapping[str, Sequence[signals.Subscriber]] = {
    "device.presence": [on_device],
    "device.capacity": [on_device],
    "wraps.stored": [on_device],
    "log.appended": [on_log],
}
