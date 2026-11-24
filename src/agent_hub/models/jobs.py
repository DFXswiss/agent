"""The job of the models area (ADR-027 section 4 `<area>/jobs.py:JOBS`, ADR-080 section 7).

`models.dfx_keys` runs every 10 s: one pass of `agent_hub.models.dfx.plan` (revoke the keys of org devices that left,
request keys for the serving org devices while billing is active). After the commit it sends the content-free hint
`reconcile {}` to the elected issuer when something changed (it then creates or deletes keys at once instead of at its
next fetch of the device state) and to every org device whose key was revoked (it drops the envelope).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_hub.billing import binding
from agent_hub.db.pool import transaction
from agent_hub.jobs import Job
from agent_hub.models import dfx
from agent_hub.stream.publish import hint

INTERVAL_SECONDS = 10.0


def keys_pass(context: Any) -> list[UUID]:
    """One pass (module docstring); the devices that were hinted."""
    binding.bind(context.config)
    now = context.clock()
    with transaction(context.pool) as connection:
        done = dfx.plan(connection, now)
        elected = dfx.issuer(connection, now, context.config.device_online_seconds)
    hinted = list(done.revoked)
    if elected is not None and (done.requested or done.revoked):
        hinted.append(elected)
    for device in hinted:
        hint(device, "reconcile")
    return hinted


JOBS = (Job("models.dfx_keys", INTERVAL_SECONDS, keys_pass, initial_delay_seconds=5.0),)
