"""Jobs of the scheduler area (ADR-027 section 4).

`scheduler.reschedule` binds the process's hub context for the signal subscribers (`agent_hub.scheduler.signals`) and
the scheduler settings (`agent_hub.scheduler.settings`), and
sweeps every waiting session: a safety net for signals that arrived before a context was bound or in another hub
process. Every 15 s (the heartbeat interval of 03-api.md section 6.2).
"""

from __future__ import annotations

from typing import Any

from agent_hub.db.pool import transaction
from agent_hub.jobs import Job
from agent_hub.scheduler import settings, signals
from agent_hub.scheduler.reschedule import reschedule, waiting_wgs

SWEEP_INTERVAL_SECONDS = 15.0


def sweep(context: Any) -> int:
    """Bind the context and place every waiting session that can be placed; the number placed."""
    signals.bind(context)
    settings.bind(context.config)
    with transaction(context.pool) as connection:
        wgs = waiting_wgs(connection, context.clock())
    return reschedule(context, wgs)


JOBS = (Job("scheduler.reschedule", SWEEP_INTERVAL_SECONDS, sweep),)
