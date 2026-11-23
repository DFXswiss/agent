"""Background job of the audit area (ADR-027 section 4): the hourly signed checkpoint of the audit head
(02-data-model.md section 2.9, `agent_hub.audit.checkpoint`).

The job runs every `CHECK_SECONDS` and writes a checkpoint of the current head when the newest checkpoint of the hub
key is at least `CHECKPOINT_INTERVAL` (one hour) old by the hub clock, or when there is none; a head that a checkpoint
already covers gets none (`write_checkpoint`). Checking often instead of sleeping an hour keeps the rhythm across
restarts: a hub that was down longer than an hour writes the missed checkpoint at its first check. Passes of several
hub processes are serialised by the advisory lock `CHECKPOINT_LOCK`, so one hour gets one checkpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import psycopg

from agent_core.signing import HubSigner
from agent_hub.audit.checkpoint import AuditCheckpoint, newest_checkpoint, write_checkpoint
from agent_hub.jobs import Job

CHECKPOINT_INTERVAL = timedelta(hours=1)
CHECK_SECONDS = 60.0
CHECKPOINT_LOCK = 742180103
"""Advisory lock of the checkpoint job (next to the migration, audit and platform-log locks 742180100-742180102)."""


def hourly_checkpoint(connection: psycopg.Connection[Any], signer: HubSigner, now: datetime) -> AuditCheckpoint | None:
    """Write the checkpoint that is due (module docstring); None when none is due or the head is covered."""
    with connection.transaction():
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (CHECKPOINT_LOCK,))
        newest = newest_checkpoint(connection, signer.key_id)
        if newest is not None and now - newest.published_at < CHECKPOINT_INTERVAL:
            return None
        return write_checkpoint(connection, signer, now)


def run_checkpoint(context: Any) -> AuditCheckpoint | None:
    """One pass of the job on the hub clock."""
    with context.pool.connection() as connection:
        return hourly_checkpoint(connection, context.signer, context.clock())


JOBS = [Job("audit.checkpoint", CHECK_SECONDS, run_checkpoint, initial_delay_seconds=CHECK_SECONDS)]
