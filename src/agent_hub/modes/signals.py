"""The chooser's rights count (ADR-098 section 3): a mode whose chooser lost `wg:write` is switched off.

Drivers act with the rights of the person who chose a mode (A15). A person loses write access to a workgroup or
leaves an organization only through the organization's membership log, so the subscriber of `log.appended` (`org_id`)
is the moment to look: every mode that is on for a task of the organization whose `chosen_by` no longer holds
`wg:write` on the task's workgroup (`agent_hub.orgs.rights.can`: removed, access revoked or narrowed to `read`, the
workgroup archived) is switched off with the reason `chooser_left` by the hub (`modes.store.switch_off`, audit
`task.mode_off`), with the `task` frame and `reconcile {}` to the task's devices after the commit.

Like `agent_hub.device.signals` the subscriber reads with the pool of the bound hub context and queues the work on one
background thread (`wait` drains it); without a bound context it does nothing (the next append looks again). A failure
is logged and never affects the append.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import UUID

from agent_hub import signals
from agent_hub.db.pool import transaction
from agent_hub.device import signals as device_signals
from agent_hub.modes import store
from agent_hub.modes.choice import announce
from agent_hub.orgs import rights
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import HUB, lock_task

LOG = logging.getLogger("agent_hub.modes")
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="modes-chooser")
_pending: list[Future[None]] = []


def revoke_left(connection: Any, org_id: UUID, now: Any, effects: Effects) -> int:
    """Switch off every mode of the org's tasks whose chooser lost `wg:write`; the number switched off."""
    rows = connection.execute(
        "SELECT m.task_id, m.mode, m.chosen_by, t.wg_id FROM hub.task_mode m JOIN hub.task t ON t.id = m.task_id"
        " WHERE t.org_id = %s AND m.state = 'on' ORDER BY m.task_id, m.mode",
        (org_id,),
    ).fetchall()
    count = 0
    for task_id, mode, chooser, wg_id in rows:
        if rights.can(connection, chooser, rights.WG_WRITE, wg_id=wg_id):
            continue
        task = lock_task(connection, task_id)
        if task is not None and store.switch_off(
            connection, task, mode, actor=HUB, now=now, reason=store.CHOOSER_LEFT
        ):
            announce(connection, effects, task)
            count += 1
    return count


def run(context: Any, org_id: UUID) -> None:
    """One revocation pass for the org (module docstring)."""
    if context.pool.closed:
        return
    try:
        effects = Effects()
        with transaction(context.pool) as connection:
            revoke_left(connection, org_id, context.clock(), effects)
        effects.flush()
    except Exception:
        LOG.error("modes of a chooser without write access could not be switched off", exc_info=True)


def on_log_appended(_name: str, ids: Mapping[str, Any]) -> None:
    """Queue a revocation pass of the org (never blocks the emitter)."""
    context = device_signals.bound()
    if context is None:
        return
    _pending[:] = [future for future in _pending if not future.done()]
    _pending.append(_worker.submit(run, context, ids["org_id"]))


def wait() -> None:
    """Wait until every queued pass has run."""
    while _pending:
        _pending.pop(0).result()


SUBSCRIBERS: Mapping[str, Sequence[signals.Subscriber]] = {"log.appended": [on_log_appended]}
