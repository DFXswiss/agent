"""Jobs of the agent area (ADR-027 section 4: `agents/jobs.py:JOBS`; 11-agent-access.md section 2).

`agents.unclaimed_purge`: open and confirmed pairings that were never claimed are deleted one hour after their claim
deadline (with the sealed identity of a confirmed one); the sealed handover of a remote pairing is cleared as soon as
its claim deadline has passed (ADR-067 section 3).
"""

from __future__ import annotations

from typing import Any

from agent_hub.agents.store import clear_expired_handovers, purge_unclaimed
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job

PURGE_INTERVAL_SECONDS = 300.0


def purge(context: Any) -> int:
    """Clear expired handovers and delete unclaimed pairings past the retention; the number of rows changed."""
    with transaction(context.pool) as connection:
        now = context.clock()
        return clear_expired_handovers(connection, now) + purge_unclaimed(connection, now)


JOBS = (Job("agents.unclaimed_purge", PURGE_INTERVAL_SECONDS, purge),)
