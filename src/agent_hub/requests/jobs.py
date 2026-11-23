"""Jobs of the requests area (ADR-027 section 4): `requests.expire` closes the device requests that are past their
expiry of 15 minutes (03-api.md sections 4.10 and 12; roots with their follow-ups) and fails the active requests whose
device is no longer eligible for their user (`device_not_assigned`). Both are audited with the hub as actor."""

from __future__ import annotations

from typing import Any

from agent_hub.db.pool import transaction
from agent_hub.jobs import Job
from agent_hub.requests.store import expire_due, fail_ineligible

EXPIRE_INTERVAL_SECONDS = 10.0


def expire(context: Any) -> int:
    """One pass; the number of requests closed."""
    now = context.clock()
    with transaction(context.pool) as connection:
        return expire_due(connection, now) + fail_ineligible(connection, now)


JOBS = (Job("requests.expire", EXPIRE_INTERVAL_SECONDS, expire),)
