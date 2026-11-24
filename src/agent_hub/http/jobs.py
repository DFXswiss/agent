"""Jobs of the request pipeline: purge of the nonce replay store (02-data-model.md section 2.1)."""

from __future__ import annotations

from typing import Any

from agent_hub.http.nonces import purge_nonces
from agent_hub.jobs import Job

PURGE_INTERVAL_SECONDS = 60.0


def purge_request_nonces(context: Any) -> int:
    """Delete nonces older than the replay window."""
    return purge_nonces(context.pool, context.clock())


JOBS = (Job("request_nonce.purge", PURGE_INTERVAL_SECONDS, purge_request_nonces),)
