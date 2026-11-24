"""The signal `invite.accepted` (ADR-027 section 4; HUB-15 turns it into notifications).

An org invitation is used by its `accept` entry, which the orgs area appends (a user through
`POST /v1/orgs/{org}/log`, a device through its enrollment) and announces with `log.appended` (`org_id`, `seq` = the
new head) after the commit. The subscriber of this module finds the `accept` entries of that batch (the run of
entries sharing the receive time of the head entry, as `agent_hub.device.signals` does) and emits
`invite.accepted` (`org_id`, `invite_id`, `subject_id`) once for each, in log order. Only identifiers travel.

A subscriber gets no app context: it reads with the pool of the hub context bound by the job
`invites.expire_packages` (`agent_hub.invites.jobs`, which runs right after the process starts its jobs). The emitter
calls subscribers on the request's event loop, so the subscriber only queues the work on one background thread
(`wait` lets tests and shutdown drain it); a failure there is logged and never reaches the request.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from uuid import UUID

from agent_hub import signals

LOG = logging.getLogger("agent_hub.invites")
ACCEPTED = "invite.accepted"
_context: dict[str, Any] = {}
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="invites-signal")
_pending: list[Future[None]] = []


def bind(context: Any) -> None:
    """Remember the hub context whose pool the subscriber reads."""
    _context["hub"] = context


def accepted_in_batch(connection: Any, org_id: UUID, seq: int) -> list[tuple[str, UUID]]:
    """`(invite_id, subject)` of the `accept` entries of the batch that ends at `seq`, in log order."""
    rows = connection.execute(
        "SELECT e.entry_text FROM hub.membership_entry e WHERE e.org_id = %s AND e.type = 'accept' AND e.seq <= %s"
        " AND e.received_at = (SELECT received_at FROM hub.membership_entry WHERE org_id = %s AND seq = %s)"
        " ORDER BY e.seq",
        (org_id, seq, org_id, seq),
    ).fetchall()
    found = []
    for (text,) in rows:
        body = json.loads(text)["body"]
        found.append((str(body["invite_id"]), UUID(str(body["subject"]))))
    return found


def announce(context: Any, org_id: UUID, seq: int) -> None:
    """Emit `invite.accepted` for every `accept` of the batch (module docstring)."""
    if context.pool.closed:
        return
    try:
        with context.pool.connection() as connection:
            accepted = accepted_in_batch(connection, org_id, seq)
        for invite_id, subject in accepted:
            signals.emit(ACCEPTED, org_id=org_id, invite_id=invite_id, subject_id=subject)
    except Exception:
        LOG.error("invite.accepted could not be announced", exc_info=True)


def on_log_appended(_name: str, ids: Mapping[str, Any]) -> None:
    """Queue the announcement of a committed append (never blocks the emitter)."""
    context = _context.get("hub")
    if context is None:
        return
    _pending[:] = [future for future in _pending if not future.done()]  # keep only the running ones
    _pending.append(_worker.submit(announce, context, ids["org_id"], ids["seq"]))


def wait() -> None:
    """Wait until every queued announcement has run."""
    while _pending:
        _pending.pop(0).result()


SUBSCRIBERS: Mapping[str, Sequence[signals.Subscriber]] = {"log.appended": [on_log_appended]}
