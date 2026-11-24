"""Used-up and refused subscription accesses, kept across restarts (KOR-50, ADR-080 section 2; ADR-052 section 6).

An **access key** names one access whose provider can run out: `own:<owner_id>` (the owner's own Claude
subscription), `operator:<file name>` (a token file of the operator's pool) and `dfxkey:<device_id>` (the limited DFX
key of an org device, ADR-080 section 7). API keys and the platform key of a platform device answer `429` for short
rate limits only and are never limited here.

`Limits` (one per daemon, `limits_for(context)`) mirrors the table `runner.access_limit`:

- `exhausted` rows: the access is used up until `until` - the latest `resets_at` of the full windows of the whole
  subscription when the quota is readable (`readable`), else `now + PROBE_S` after a streak of `429` answers
  (`model.exhaustion`), or `now + DFX_KEY_S` after a `402` of the DFX key. `active(key)` is the row while its
  `until` lies ahead; a row whose `until` passed is released (deleted at the next load or `prune`), so the access is
  tried again by the next start or resume;
- `blocked` rows: the ADR-052 `401`/`403` block of an operator token file with the identity of the file's content it
  was found for (`model.operator` keeps them; a new content releases the block).

The rows are read once per daemon (`load`); rows whose `until` passed are deleted then. The memory also holds the
latest quota reading of each access (`readings`, never a token) and a change counter for the operator quota
snapshot (`version`).

`full_until(windows, now)` is the rule of a readable quota: only the windows of the whole subscription count
(`five_hour`, `seven_day`; a per-model `weekly:<model>` window does not, Claude Code changes the model itself); the
access is used up when one of them reports `used_percent >= 100` with `resets_at` in the future, until the latest
such `resets_at`. `fullest(windows)` is the highest `used_percent` of those windows (the operator pool chooses the
file with the most room first).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_runner.orgs.db import session as store_session
from agent_runner.quota.reader import Reading

ATTRIBUTE = "model_limits"
CHECK_S = 60.0
"""The usage endpoint is read for one access at most once in this time after a `429`."""
STREAK = 3
STREAK_S = 300.0
PROBE_S = 900.0
DFX_KEY_S = 120.0
RECHECK_S = 300.0
"""An own credential that cannot be used on this device right now is tried again after this time."""
WHOLE = ("five_hour", "seven_day")
EXHAUSTED = "exhausted"
BLOCKED = "blocked"
OWN = "own:"
OPERATOR = "operator:"
DFX_KEY = "dfxkey:"


def own_key(owner_id: str) -> str:
    return OWN + owner_id


def operator_key(name: str) -> str:
    return OPERATOR + name


def dfx_key(device_id: str) -> str:
    return DFX_KEY + device_id


@dataclass(frozen=True)
class Limit:
    key: str
    kind: str
    until: float | None
    readable: bool
    limited_at: float
    identity: str | None = None


@dataclass
class Limits:
    """The limits of one daemon (module docstring)."""

    wall: Callable[[], float] = time.time
    rows: dict[str, Limit] = field(default_factory=dict)
    loaded: bool = False
    readings: dict[str, Reading] = field(default_factory=dict)
    version: int = 0

    def active(self, key: str, now: float | None = None) -> Limit | None:
        """The `exhausted` row of `key` while its `until` lies ahead, else None."""
        row = self.rows.get(key)
        if row is None or row.kind != EXHAUSTED:
            return None
        assert row.until is not None
        return row if row.until > (self.wall() if now is None else now) else None

    def blocks(self) -> dict[str, str]:
        """Operator file name -> identity text of the persisted `401`/`403` blocks."""
        return {
            key[len(OPERATOR) :]: row.identity
            for key, row in self.rows.items()
            if row.kind == BLOCKED and row.identity is not None
        }


def limits_for(context: Any) -> Limits:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Limits):
        return found
    created = Limits()
    setattr(context, ATTRIBUTE, created)
    return created


def _stamp(value: float | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value, UTC)


async def load(context: Any) -> Limits:
    """The limits of the store, once per daemon; rows whose `until` passed are released."""
    found = limits_for(context)
    if found.loaded:
        return found
    now = _stamp(found.wall())
    async with store_session(context.pool) as conn:
        await conn.execute("DELETE FROM runner.access_limit WHERE kind = 'exhausted' AND until <= %s", (now,))
        cursor = await conn.execute(
            "SELECT access_key, kind, until, readable, limited_at, identity FROM runner.access_limit"
        )
        rows = await cursor.fetchall()
    for key, kind, until, readable, limited_at, identity in rows:
        found.rows[key] = Limit(
            key=key,
            kind=kind,
            until=None if until is None else until.timestamp(),
            readable=readable,
            limited_at=limited_at.timestamp(),
            identity=identity,
        )
    found.loaded = True
    found.version += 1
    return found


async def mark(context: Any, limit: Limit) -> None:
    """Record `limit` (replaces an earlier row of its key)."""
    found = await load(context)
    async with store_session(context.pool) as conn:
        await conn.execute(
            "INSERT INTO runner.access_limit (access_key, kind, until, readable, limited_at, identity)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (access_key) DO UPDATE SET kind = EXCLUDED.kind,"
            " until = EXCLUDED.until, readable = EXCLUDED.readable, limited_at = EXCLUDED.limited_at,"
            " identity = EXCLUDED.identity",
            (limit.key, limit.kind, _stamp(limit.until), limit.readable, _stamp(limit.limited_at), limit.identity),
        )
    found.rows[limit.key] = limit
    found.version += 1


async def release(context: Any, keys: Iterable[str]) -> None:
    """Drop the rows of `keys` (released accesses, blocks of a renewed file)."""
    found = await load(context)
    gone = [key for key in keys if key in found.rows]
    if not gone:
        return
    async with store_session(context.pool) as conn:
        await conn.execute("DELETE FROM runner.access_limit WHERE access_key = ANY(%s)", (gone,))
    for key in gone:
        del found.rows[key]
    found.version += 1


async def prune(context: Any) -> None:
    """Release the `exhausted` rows whose `until` passed."""
    found = await load(context)
    now = found.wall()
    await release(context, [key for key, row in found.rows.items() if row.until is not None and row.until <= now])


def resets(window: Mapping[str, Any]) -> float | None:
    """`resets_at` of a window (contract time) as Unix time, None when absent."""
    value = window.get("resets_at")
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def full_until(windows: Iterable[Mapping[str, Any]], now: float) -> float | None:
    """The release time of a readable quota that is used up, else None (module docstring)."""
    found = [
        when
        for window in windows
        if window.get("window") in WHOLE and window.get("used_percent", 0) >= 100
        for when in [resets(window)]
        if when is not None and when > now
    ]
    return max(found) if found else None


def fullest(windows: Iterable[Mapping[str, Any]]) -> float | None:
    """The highest `used_percent` of the windows of the whole subscription, None without one."""
    found = [float(window["used_percent"]) for window in windows if window.get("window") in WHOLE]
    return max(found) if found else None


async def apply(context: Any, key: str, reading: Reading) -> Limit | None:
    """A quota reading of `key` (exhaustion check or quota poll): a full window marks the access used up until the
    provider's release time; a readable quota that is no longer full releases a mark that came from a readable quota
    (a streak mark keeps its probe time: the figures may lag). Returns the new mark, else None."""
    found = await load(context)
    found.readings[key] = reading
    found.version += 1
    if not reading.readable:
        return None
    until = full_until(reading.windows, reading.fetched_at)
    current = found.rows.get(key)
    if until is not None:
        if current is not None and current.kind == EXHAUSTED and current.readable and current.until == until:
            return None
        limit = Limit(key=key, kind=EXHAUSTED, until=until, readable=True, limited_at=reading.fetched_at)
        await mark(context, limit)
        return limit
    if current is not None and current.kind == EXHAUSTED and current.readable:
        await release(context, [key])
    return None
