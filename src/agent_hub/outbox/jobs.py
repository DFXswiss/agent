"""Jobs of the outbox area (ADR-027 section 4): pruning of old outbox receipts (ADR-030).

`outbox.prune_receipts` runs hourly and deletes receipts older than `RECEIPT_RETENTION` except the newest receipt of
every device; a resend of a pruned `origin_seq` is answered `replayed` (`agent_hub.outbox.process`).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_hub.jobs import Job

RECEIPT_RETENTION = timedelta(days=7)
PRUNE_INTERVAL_SECONDS = 3600.0


def prune_receipts(context: Any) -> int:
    """Delete the old receipts; returns how many were deleted."""
    cutoff = context.clock() - RECEIPT_RETENTION
    with context.pool.connection() as connection, connection.transaction():
        deleted = connection.execute(
            "DELETE FROM hub.outbox_receipt r WHERE r.received_at < %s AND r.origin_seq < (SELECT MAX(origin_seq)"
            " FROM hub.outbox_receipt n WHERE n.device_id = r.device_id)",
            (cutoff,),
        )
        return int(deleted.rowcount)


JOBS = (Job("outbox.prune_receipts", PRUNE_INTERVAL_SECONDS, prune_receipts, initial_delay_seconds=60.0),)
