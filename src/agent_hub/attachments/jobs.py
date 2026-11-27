"""Jobs of the attachments area (ADR-027 section 4): `attachments.expire` removes the uploads of attachments that
received no chunk for 6 hours (a browser that gave up: a new attempt uploads a new object; ADR-102 section 3). A
prompt names only complete objects, so removing such an upload loses nothing."""

from __future__ import annotations

import logging
from typing import Any

from agent_hub.attachments.store import expire as expire_uploads
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job

LOG = logging.getLogger("agent_hub.attachments")
EXPIRE_INTERVAL_SECONDS = 600.0


def expire(context: Any) -> int:
    """One pass; the number of uploads removed (logged when there were some)."""
    with transaction(context.pool) as connection:
        removed = expire_uploads(connection, context.clock())
    if removed:
        LOG.info("attachments.expire removed=%d", removed)
    return removed


JOBS = (Job("attachments.expire", EXPIRE_INTERVAL_SECONDS, expire),)
