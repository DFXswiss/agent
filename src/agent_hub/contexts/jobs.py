"""Jobs of the contexts area (ADR-027 section 4): `contexts.expire` removes the uploads of context checkpoints that
received no chunk for 6 hours (an upload the runner abandoned: it retries with a new object; ADR-058 section 6). A
restore never sees such an object (only complete checkpoints are read), so removing it loses nothing."""

from __future__ import annotations

import logging
from typing import Any

from agent_hub.contexts.store import expire as expire_uploads
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job

LOG = logging.getLogger("agent_hub.contexts")
EXPIRE_INTERVAL_SECONDS = 600.0


def expire(context: Any) -> int:
    """One pass; the number of uploads removed (logged when there were some)."""
    with transaction(context.pool) as connection:
        removed = expire_uploads(connection, context.clock())
    if removed:
        LOG.info("contexts.expire removed=%d", removed)
    return removed


JOBS = (Job("contexts.expire", EXPIRE_INTERVAL_SECONDS, expire),)
