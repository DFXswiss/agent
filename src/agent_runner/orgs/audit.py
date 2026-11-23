"""Audit and alerts of inbound verification (05-runner.md sections 2.2 and 2.4, 03-api.md section 6.1).

Anything the runner refuses to act on is ignored, audited and counted:

- `reject(...)`: outbox item `audit` with action `runner.inbound_rejected`, result `rejected` and the metadata
  `{source, code}` (never content), the counter `verify_failures` (metric `agent_runner_device_verify_failures_total`,
  exported by RUN-15) and one log line `runner.inbound rejected source=... code=...`.
- `log_alert(...)`: a membership-log or platform-log rollback or fork: log line `runner.alert <code> ...`, outbox items
  `runner.alert` (`{alert, value}`) and `runner.log_rollback` / `runner.log_fork` (`{verified_seq, offered_seq}`).

Both write the outbox items in the caller's transaction (`SyncRuntime.enqueue`). `counters(context)` is the
per-daemon counter map, kept on the daemon context like the sync runtime.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sync.runtime import SyncRuntime

SOURCES = frozenset({"event", "log", "platform_log", "wrap", "request", "assignment", "setting", "credential"})
LOG_ALERTS = {"log_rollback": "runner.log_rollback", "log_fork": "runner.log_fork"}
VERIFY_FAILURES = "verify_failures"
LOG_ALERTS_COUNTER = "log_alerts"
ATTRIBUTE = "runner_counters"
_TARGET_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TARGET_KIND_RE = re.compile(r"[a-z_]{1,32}")


def counters(context: Any) -> Counter[str]:
    """The counters of this daemon (created on first use)."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Counter):
        return found
    created: Counter[str] = Counter()
    setattr(context, ATTRIBUTE, created)
    return created


def _line(context: Any, text: str) -> None:
    context.log.write(text + "\n")
    context.log.flush()


def _target(kind: str, target_id: str) -> tuple[str, str]:
    if not _TARGET_KIND_RE.fullmatch(kind):
        raise ValueError("target kind must be lower snake case")
    return kind, target_id if _TARGET_ID_RE.fullmatch(target_id) else "unknown"


async def reject(
    context: Any,
    sync: SyncRuntime,
    conn: AsyncConnection[Any],
    *,
    source: str,
    code: str,
    target_kind: str,
    target_id: str,
) -> None:
    """Audit and count one refused inbound item (in the caller's transaction)."""
    if source not in SOURCES:
        raise ValueError(f"unknown inbound source: {source!r}")
    kind, target = _target(target_kind, target_id)
    body = {
        "action": "runner.inbound_rejected",
        "result": "rejected",
        "target_kind": kind,
        "target_id": target,
        "detail": {"source": source, "code": code},
    }
    await sync.enqueue(conn, "audit", body)
    counters(context)[VERIFY_FAILURES] += 1
    _line(context, f"runner.inbound rejected source={source} code={code} target={kind}:{target}")


async def log_alert(
    context: Any,
    sync: SyncRuntime,
    conn: AsyncConnection[Any],
    *,
    code: str,
    target_kind: str,
    target_id: str,
    verified_seq: int,
    offered_seq: int,
) -> None:
    """Alert and audit a log rollback or fork (in the caller's transaction)."""
    action = LOG_ALERTS[code]
    kind, target = _target(target_kind, target_id)
    alert = {
        "action": "runner.alert",
        "result": "failed",
        "target_kind": kind,
        "target_id": target,
        "detail": {"alert": code, "value": None},
    }
    audit = {
        "action": action,
        "result": "rejected",
        "target_kind": kind,
        "target_id": target,
        "detail": {"verified_seq": verified_seq, "offered_seq": offered_seq},
    }
    await sync.enqueue(conn, "audit", alert)
    await sync.enqueue(conn, "audit", audit)
    counters(context)[LOG_ALERTS_COUNTER] += 1
    _line(context, f"runner.alert {code} {kind}={target} verified_seq={verified_seq} offered_seq={offered_seq}")
