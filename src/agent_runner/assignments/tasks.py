"""Daemon task `revocation` (05-runner.md sections 1.1 and 3.5, 03-api.md sections 6.1 and 6.2).

A removed org device and a decertified platform device are refused by the hub (`401 key_revoked` on every signed
request), so their reconcile runs fail at step 1 and never reach step 4; a platform device whose verified platform log
shows its `device_decertify` halts at step 2 (`decertified`). This task watches the outcome of every reconcile run
(`Engine.last`, polled every `POLL_S` on the host clock) and ends everything on this device (`teardown.revoke`):

- a run halted `decertified` (verified by the platform log): revoke at once;
- a run that failed: one signed `GET /v1/device/state` probe; a verified refusal `key_revoked` revokes once it was
  confirmed by probes of failed runs for at least `CONFIRM_S` (60 s) without a successful run in between, so one
  wrong answer of a hub never ends anything; anything else (hub unreachable, another refusal, a successful run) resets
  the confirmation;
- a revocation persisted by an earlier daemon is repeated once at start (the teardown is idempotent).

The teardown holds the engine's lock, and the gate step `revoked` (order 5, `assignments/reconcile.py`) halts every
later run, so a revoked device neither interleaves a reconcile run with the teardown nor rebuilds keys, sessions or
VMs afterwards. The `decertified` hint (registered by the reconcile area) wakes the engine.
The task ends at once on a device that is not enrolled.
"""

from __future__ import annotations

from typing import Any

from agent_runner.assignments import teardown
from agent_runner.reconcile.engine import engine_for
from agent_runner.registry import TaskSpec
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import runtime

POLL_S = 2.0
CONFIRM_S = 60.0
PROBE_TARGET = "/v1/device/state"


async def probe(context: Any) -> bool:
    """True when the hub refuses this device's key (`key_revoked`)."""
    sync = runtime(context)
    assert sync is not None
    try:
        await sync.client.request("GET", PROBE_TARGET)
    except HubRefusal as refusal:
        return refusal.code == teardown.KEY_REVOKED
    except HubTransportError:
        return False
    return False


async def _revoke(context: Any, engine: Any, reason: str) -> None:
    """The teardown under the engine's lock: no reconcile run (session starts, wraps, catch-up) interleaves, and the
    gate step halts every later run."""
    async with engine.lock:
        await teardown.revoke(context, reason)


async def run_revocation(context: Any) -> None:
    engine = engine_for(context)
    if engine is None:
        return
    clock = context.host.clock
    previous = await teardown.revoked(context)
    if previous is not None:
        await _revoke(context, engine, previous)
    done = previous is not None
    seen = 0
    first: float | None = None
    while not context.stopping.is_set() and not done:
        last = engine.last
        if last is not None and engine.runs != seen:
            seen = engine.runs
            if last.halted == teardown.DECERTIFIED:
                await _revoke(context, engine, teardown.DECERTIFIED)
                done = True
            elif last.halted == "failed" and await probe(context):
                first = clock() if first is None else first
                if clock() - first >= CONFIRM_S:
                    await _revoke(context, engine, teardown.KEY_REVOKED)
                    done = True
                else:
                    _line(context, f"key_revoked_seen confirm_after_s={round(CONFIRM_S - (clock() - first))}")
            else:
                first = None
        if not done:
            await context.host.sleep(POLL_S)
    await context.stopping.wait()


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.assignments {text}\n")
    context.log.flush()


TASKS = [TaskSpec("revocation", run_revocation)]
