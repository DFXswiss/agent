"""Exhaustion of a subscription access: detected from the gateway's `usage` records, confirmed by the provider where
possible (KOR-50, ADR-081 section 2).

`observe(env, session, status, key, counts)` is called by the `usage` journal handler for every record of a session
whose access can run out (`access_key`: `own:<owner_id>` for the owner's own subscription, `operator:<file>` for a
file of the operator's pool, `dfxkey:<device_id>` for the DFX key of an org device):

- a successful record (`2xx`) ends the access's streak;
- **trigger** `429` of a subscription access: the check runs in a task of its own (never inside the journal's
  transaction), and the record joins the access's streak - for an operator file only when the pool holds it against
  the file (`operator.Pool.observe`), so a streak counts against the ADR-052 limit of the session that completed it:
  * *readable quota* - the usage endpoint is read with the access's token at once, at most once per access per
    `limits.CHECK_S`; a full window of the whole subscription marks the access used up until the provider's release
    time (`limits.apply`);
  * *streak* - otherwise (not readable, or below 100 % because the figures lag) `limits.STREAK` records within
    `limits.STREAK_S` without a successful one in between mark it until `now + limits.PROBE_S`. A single `429` marks
    nothing (Claude Code retries it); an access that is marked already is not marked again by a streak;
- `402` of a `platform` session of an org device (its DFX key reached the limit the issuer set): `dfxkey:<device_id>`
  is marked until `now + limits.DFX_KEY_S`.

A mark of the owner's own subscription is reported at once to a hub with `model_fallback.api` as a `quota` item with
`limited` (the windows of the reading, or `unavailable: true` when the endpoint refused the token). Tokens are used for
one request and never logged or stored. `settle(context)` waits for the running checks (tests, shutdown).
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from agent_runner.credentials import cache
from agent_runner.model import limits, operator, policy, provision
from agent_runner.model.limits import Limit
from agent_runner.orgs.db import session as store_session
from agent_runner.quota import reader
from agent_runner.sessions.records import LocalSession, time_text
from agent_runner.sync.runtime import runtime

ATTRIBUTE = "model_exhaustion"
LIMITED = 429
PAYMENT = 402
KIND = "claude_subscription"


@dataclass
class Watch:
    """Per daemon: the streak of each access, the time of its last check and the running checks."""

    streaks: dict[str, list[float]] = field(default_factory=dict)
    checked_at: dict[str, float] = field(default_factory=dict)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)


def watch_for(context: Any) -> Watch:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Watch):
        return found
    created = Watch()
    setattr(context, ATTRIBUTE, created)
    return created


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.model {text}\n")
    context.log.flush()


def access_key(env: Any, session: LocalSession, bound: str | None) -> str | None:
    """The access key of a session's current access, None when it cannot run out (API key, platform key)."""
    if session.model_access == "platform":
        return limits.dfx_key(env.device_id) if env.device_kind == "org" else None
    if session.model_access == "subscription":
        return limits.operator_key(bound) if bound is not None else limits.own_key(session.owner_id or "")
    return None


def _status(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def observe(env: Any, status: object, key: str | None, counts: bool = True) -> None:
    """One `usage` record of a session on the access `key` (module docstring)."""
    context = env.context
    code = _status(status)
    if context is None or key is None or code is None:
        return
    found = watch_for(context)
    if 200 <= code < 300:
        found.streaks.pop(key, None)
        return
    now = limits.limits_for(context).wall()
    if code == LIMITED and not key.startswith(limits.DFX_KEY):
        if counts:
            found.streaks[key] = [at for at in found.streaks.get(key, []) if now - at < limits.STREAK_S] + [now]
        schedule(context, confirm(env, key))
    elif code == PAYMENT and key.startswith(limits.DFX_KEY):
        schedule(context, mark_dfx_key(context, key))


def schedule(context: Any, work: Coroutine[Any, Any, None]) -> None:
    found = watch_for(context)
    task = asyncio.ensure_future(work)
    found.tasks.add(task)

    def done(finished: asyncio.Task[None]) -> None:
        found.tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            _line(context, f"exhaustion_check_failed error={type(finished.exception()).__name__}")

    task.add_done_callback(done)


async def settle(context: Any) -> None:
    """Wait until no check runs."""
    found = watch_for(context)
    while found.tasks:
        await asyncio.gather(*list(found.tasks), return_exceptions=True)


async def token_of(context: Any, key: str) -> str | None:
    """The token of a subscription access for one read of its quota, None when it cannot be had now."""
    if key.startswith(limits.OPERATOR):
        pool = operator.pool_for(context)
        entry = None if pool is None else pool.entries().get(key[len(limits.OPERATOR) :])
        return None if entry is None else entry.token
    try:
        async with cache.opened(context, key[len(limits.OWN) :], KIND) as body:
            return provision.owner_secret(KIND, body)
    except Exception:
        return None


def note_quota(context: Any, key: str, reading: reader.Reading) -> None:
    """The room of an operator file for the pool's choice (`operator.Pool.quota`)."""
    pool = operator.pool_for(context) if key.startswith(limits.OPERATOR) else None
    if pool is None:
        return
    name = key[len(limits.OPERATOR) :]
    used = limits.fullest(reading.windows) if reading.readable else None
    if used is None:
        pool.quota.pop(name, None)
    else:
        pool.quota[name] = used


async def confirm(env: Any, key: str) -> None:
    """The check after a `429` of `key` (module docstring)."""
    context = env.context
    found = watch_for(context)
    async with found.locks.setdefault(key, asyncio.Lock()):
        known = await limits.load(context)
        now = known.wall()
        marked: Limit | None = None
        if now - found.checked_at.get(key, float("-inf")) >= limits.CHECK_S:
            found.checked_at[key] = now
            token = await token_of(context, key)
            reading = None if token is None else await reader.read(context, token, now)
            if reading is not None:
                note_quota(context, key, reading)
                marked = await limits.apply(context, key, reading)
        streak = [at for at in found.streaks.get(key, []) if now - at < limits.STREAK_S]
        if marked is None and len(streak) >= limits.STREAK and known.active(key, now) is None:
            marked = Limit(key=key, kind=limits.EXHAUSTED, until=now + limits.PROBE_S, readable=False, limited_at=now)
            await limits.mark(context, marked)
        if marked is None:
            return
        found.streaks.pop(key, None)
        assert marked.until is not None
        _line(
            context,
            f"access_exhausted key={key} until={time_text(marked.until)} readable={str(marked.readable).lower()}",
        )
        if key.startswith(limits.OWN):
            await report_own(env, key[len(limits.OWN) :], marked)


async def mark_dfx_key(context: Any, key: str) -> None:
    """A `402` of the DFX key of this org device (module docstring)."""
    known = await limits.load(context)
    now = known.wall()
    limit = Limit(key=key, kind=limits.EXHAUSTED, until=now + limits.DFX_KEY_S, readable=False, limited_at=now)
    await limits.mark(context, limit)
    _line(context, f"access_exhausted key={key} until={time_text(now + limits.DFX_KEY_S)} readable=false")


def limited_body(limit: Limit) -> dict[str, Any]:
    """`limited` of the `quota` item (ADR-081 section 6)."""
    return {
        "at": time_text(limit.limited_at),
        "until": None if limit.until is None else time_text(limit.until),
        "readable": limit.readable,
    }


def quota_body(owner: str, reading: reader.Reading | None, limit: Limit) -> dict[str, Any] | None:
    """The `quota` item of a marked own subscription, None without a reading to carry it."""
    if reading is None or (reading.readable and not reading.windows):
        return None
    body: dict[str, Any] = {"owner_id": owner, "fetched_at": time_text(reading.fetched_at)}
    if reading.readable:
        body["windows"] = [dict(window) for window in reading.windows]
    else:
        body["unavailable"] = True
    body["limited"] = limited_body(limit)
    return body


async def report_own(env: Any, owner: str, limit: Limit) -> None:
    context = env.context
    sync = runtime(context)
    if sync is None or not policy.api(env.desired):
        return
    body = quota_body(owner, limits.limits_for(context).readings.get(limits.own_key(owner)), limit)
    if body is None:
        return
    async with store_session(context.pool) as conn, conn.transaction():
        await sync.enqueue(conn, "quota", body)
    sync.pusher.notify()
