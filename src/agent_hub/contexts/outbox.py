"""Outbox handler extension of the contexts area (ADR-027 section 4 `<area>/outbox.py:HANDLERS`, ADR-030, ADR-072
section 7).

It runs after the base handler of the item kind `audit` (which checked the action, its detail and `device:session`
and wrote the audit row) in the same savepoint. For the two restore outcomes a runner reports on a new main session

- `session.context_restored {object_id, workspace, stale}`: `context_restore = restored`; the reason is
  `workspace_partial` or `workspace_omitted` when the workspace came back `partial` or not at all (`omitted`: too
  large), else `golden_switch` when the detail says `moved: golden` (ADR-093 section 4), none otherwise (`stale` stays
  in the audit row),
- `session.context_fallback {reason}`: `context_restore = fallback`, `context_reason = reason` (the reported emergency
  path: the session continued with a new conversation),

it stores the outcome on the session row (the newest report wins), records the `session` frame for the org's
browsers (row and newest transition, as the other session frames) and, after the commit, counts it: the log line
`metric agent_hub_session_context_total result=<restored|fallback> reason=<reason or none>` and the process counter
`COUNTS` (the hub has no Prometheus exposition; the durable record is the audit row). Other audit actions pass
unchanged. The base handler already refused a session that is unknown or not the device's; both actions need
`session_id` (`400 request_invalid` otherwise, the item is `rejected`).
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from agent_hub.errors import HubError
from agent_hub.outbox.base import Handler, ItemContext
from agent_hub.outbox.bodies import AuditBody
from agent_hub.sessions import rows

LOG = logging.getLogger("agent_hub.contexts")
METRIC = "agent_hub_session_context_total"
OUTCOMES = {"session.context_restored": "restored", "session.context_fallback": "fallback"}
WORKSPACE_REASONS = {"partial": "workspace_partial", "omitted": "workspace_omitted"}
"""The `workspace` of a restore that the session row names as its `context_reason`."""
MOVED_REASON = "golden_switch"
"""`context_reason` of a complete restore after the session's VM was switched to a newer golden (ADR-093 section 4)."""
COUNTS: Counter[tuple[str, str]] = Counter()
_lock = threading.Lock()


def count(result: str, reason: str) -> None:
    """Count one outcome (after the commit) and write the metric line."""
    with _lock:
        COUNTS[(result, reason)] += 1
    LOG.info("metric %s result=%s reason=%s", METRIC, result, reason)


def restore_outcome(item: ItemContext, body: AuditBody) -> None:
    """Store a reported restore outcome on the session row (module docstring)."""
    result = OUTCOMES.get(body.action)
    if result is None:
        return
    if body.session_id is None:
        raise HubError("request_invalid")
    reason = body.detail.get("reason") if result == "fallback" else WORKSPACE_REASONS.get(body.detail["workspace"])
    if result == "restored" and reason is None and body.detail.get("moved") == "golden":
        reason = MOVED_REASON
    row = cast(
        tuple[UUID],
        item.connection.execute(
            "UPDATE hub.session SET context_restore = %s, context_reason = %s WHERE id = %s RETURNING org_id",
            (result, reason, body.session_id),
        ).fetchone(),
    )
    item.effects.org_frame(row[0], "session", rows.session_frame(item.connection, str(body.session_id)))
    item.after.append(lambda: count(result, reason or "none"))


HANDLERS: Mapping[str, Handler] = {"audit": restore_outcome}
