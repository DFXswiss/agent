"""The quotas of the operator's subscriptions (KOR-50, ADR-081 sections 2, 3 and 6; ADR-052).

On a platform device with the operator's pool (`model.operator`), the task `quota` polls every token file's quota
like an owner's subscription:

- `OperatorPoller.tick(now)`: every `POLL_S` per file (a renewed file at once) the usage endpoint is read with the
  file's token (`quota.reader`); the reading marks the file used up or releases it by the rule of a readable quota
  (`model.limits.apply`) and gives the pool the room of the file for its choice (`model.exhaustion.note_quota`). A
  check after a `429` reads the file too (`model.exhaustion`), at most once per minute.
- `report(context, desired, now)`: one outbox item `operator_quota` - the whole snapshot of the device's pool - when
  something changed since the last one, at most every `REPORT_S`, and only towards a hub whose device state carries
  `model_fallback.api` (`model.policy.api`). Per file: its name (the label the operator chose), its state (`usable`,
  `exhausted`, `blocked`, `deferred`, `ignored`), whether its quota is readable and its windows, the time it was
  limited and its release time, the time of the reading - never a token.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runner.model import exhaustion, limits, operator, policy
from agent_runner.orgs.db import session as store_session
from agent_runner.quota import reader
from agent_runner.sessions.records import time_text
from agent_runner.sync.runtime import runtime

POLL_S = 900.0
REPORT_S = 60.0
MAX_FILES = 64
MAX_WINDOWS = 16
ATTRIBUTE = "quota_operator_report"


@dataclass
class OperatorPoller:
    context: Any
    next_at: dict[str, tuple[float, operator.Identity]] = field(default_factory=dict)

    async def tick(self, now: float) -> None:
        pool = operator.pool_for(self.context)
        if pool is None:
            return
        await operator.load(self.context, pool)
        pool.scan()
        for name in [name for name in self.next_at if name not in pool.files]:
            del self.next_at[name]
        for name, entry in pool.files.items():
            if entry.token is None or entry.identity is None:
                continue
            due = self.next_at.get(name)
            if due is not None and due[1] == entry.identity and now < due[0]:
                continue
            self.next_at[name] = (now + POLL_S, entry.identity)
            key = limits.operator_key(name)
            reading = await reader.read(self.context, entry.token, now)
            if reading is None:
                continue
            exhaustion.note_quota(self.context, key, reading)
            await limits.apply(self.context, key, reading)


@dataclass
class Reported:
    body: list[dict[str, Any]] | None = None
    at: float = float("-inf")


def snapshot(context: Any) -> list[dict[str, Any]] | None:
    """The subscriptions of the pool as `operator_quota` lists them, None without token files (no directory)."""
    pool = operator.pool_for(context)
    if pool is None or pool.directory is None:
        return None
    found = limits.limits_for(context)
    listed: list[dict[str, Any]] = []
    for name in sorted(pool.files)[:MAX_FILES]:
        key = limits.operator_key(name)
        reading = found.readings.get(key)
        row = found.rows.get(key)
        limited = row if row is not None and (row.kind == limits.BLOCKED or found.active(key) is not None) else None
        listed.append(
            {
                "name": name,
                "state": pool.state(name),
                "readable": reading is not None and reading.readable,
                "windows": [] if reading is None else [dict(window) for window in reading.windows[:MAX_WINDOWS]],
                "limited_at": None if limited is None else time_text(limited.limited_at),
                "until": None if limited is None or limited.until is None else time_text(limited.until),
                "fetched_at": None if reading is None else time_text(reading.fetched_at),
            }
        )
    return listed


async def report(context: Any, desired: Mapping[str, Any] | None, now: float) -> bool:
    """One `operator_quota` item when the snapshot changed (module docstring); True when one was written."""
    sync = runtime(context)
    if sync is None or not policy.api(desired):
        return False
    body = snapshot(context)
    found = getattr(context, ATTRIBUTE, None)
    reported = found if isinstance(found, Reported) else Reported()
    setattr(context, ATTRIBUTE, reported)
    if body is None or body == reported.body or now - reported.at < REPORT_S:
        return False
    async with store_session(context.pool) as conn, conn.transaction():
        await sync.enqueue(conn, "operator_quota", {"at": time_text(now), "subscriptions": body})
    sync.pusher.notify()
    reported.body, reported.at = body, now
    return True
