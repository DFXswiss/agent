"""Jobs of the telemetry area (ADR-027 section 4: `telemetry/jobs.py:JOBS`; ADR-086 section 5).

`telemetry.ui_health_purge`: the daily UI health counters older than `store.RETENTION_DAYS` (90 days, the longest
range the operator reads) are deleted once an hour.
"""

from __future__ import annotations

from typing import Any

from agent_hub.db.pool import transaction
from agent_hub.jobs import Job
from agent_hub.telemetry.store import purge

PURGE_INTERVAL_SECONDS = 3600.0


def purge_pass(context: Any) -> int:
    """Delete the counters past the retention; the number of rows deleted."""
    with transaction(context.pool) as connection:
        return purge(connection, context.clock().date())


JOBS = (Job("telemetry.ui_health_purge", PURGE_INTERVAL_SECONDS, purge_pass),)
