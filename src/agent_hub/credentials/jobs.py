"""Jobs of the credentials area (ADR-027 section 4): `credentials.prune_notices` removes the change notices of devices
that are no longer eligible for the owner (`agent_hub.credentials.notices`)."""

from __future__ import annotations

from typing import Any

from agent_hub.credentials.notices import prune
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job

PRUNE_INTERVAL_SECONDS = 3600.0


def prune_notices(context: Any) -> int:
    """One pass; the number of notices removed."""
    with transaction(context.pool) as connection:
        return prune(connection)


JOBS = (Job("credentials.prune_notices", PRUNE_INTERVAL_SECONDS, prune_notices),)
