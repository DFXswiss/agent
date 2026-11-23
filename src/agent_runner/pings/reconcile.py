"""Reconcile step of the ping area (`<area>/reconcile.py:STEPS`, 05-runner.md sections 2.2 and 9; order number 70).

The `pings` of `GET /v1/device/state` are the hub's undelivered `session_ping` rows of the sessions on this device
(`{ping_id, session_id, kind, level, ref, created_at}`, 03-api.md section 6.1). Scope: a ping is taken only for a
session this device runs that has not ended (not `stopping`, `stopped` or `failed`); every other entry is skipped,
as is an entry of the wrong shape (with a fact line; the rest of the list still counts). Taken pings are stored
(idempotent by `ping_id`, `send.Pinger.store`); then every unsent ping of a session that can take it is sent
(`send.Pinger.flush`), which also retries the runner's own pings that found no running guest before. A listed ping
stays listed until the guest journaled its delivery and the outbox audit `ping.delivered` reached the hub.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runner.pings.send import Pinger, PingInvalid, pinger_for
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sessions import records

ENDED = frozenset({"stopping", *records.FINAL})


async def apply_desired(pinger: Pinger, desired: Mapping[str, Any]) -> int:
    """Store the listed pings of active local sessions, then send what is unsent; the number of pings stored."""
    listed = wire.array(desired.get("pings", []), "pings")
    taken = 0
    for entry in listed:
        try:
            item = wire.obj(entry, "ping")
            session_id = wire.uuid_text(item.get("session_id"), "ping")
            kind, level, ref, ping_id = item.get("kind"), item.get("level"), item.get("ref"), item.get("ping_id")
            if not (isinstance(kind, str) and isinstance(ref, str) and isinstance(ping_id, str)) or not (
                level is None or isinstance(level, str)
            ):
                raise PingInvalid("ping")
            async with pinger.env.transaction() as conn:
                session = await records.get(conn, session_id)
            if session is None or session.state in ENDED:
                pinger.line(f"out_of_scope ping_id={ping_id} session_id={session_id}")
                continue
            if await pinger.store(session_id, kind, level, ref, ping_id) is not None:
                taken += 1
        except (wire.ResponseInvalid, PingInvalid):
            pinger.line("ping_invalid")
    await pinger.flush()
    return taken


async def pings_step(run: Run) -> None:
    if run.desired is None:
        return
    pinger = pinger_for(run.context)
    if pinger is not None:
        await apply_desired(pinger, run.desired)


STEPS = [ReconcileStep(70, "pings", pings_step)]
