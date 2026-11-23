"""Jobs of the agent area (ADR-027 section 4: `agents/jobs.py:JOBS`; 11-agent-access.md section 2).

`agents.unclaimed_purge`: open and confirmed pairings that were never claimed are deleted one hour after their claim
deadline (with the sealed identity of a confirmed one).
"""

from __future__ import annotations

from typing import Any

from agent_hub.agents.store import purge_unclaimed
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job

PURGE_INTERVAL_SECONDS = 300.0


def purge(context: Any) -> int:
    """Delete unclaimed pairings past the retention; the number deleted."""
    with transaction(context.pool) as connection:
        return purge_unclaimed(connection, context.clock())


JOBS = (Job("agents.unclaimed_purge", PURGE_INTERVAL_SECONDS, purge),)
