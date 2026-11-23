"""Reconcile step 56: a session that stays `agent_starting` fails with `agent_start_failed` (05-runner.md section
6.1 step 6; AUD-01 A-05, decisions/KOR-02.md).

The start marks a session `agent_starting` once the guest holds its credential; only the guest journal moves it on
(`prompt_delivering` when the TUI is up, `working` when the initial prompt was accepted, `failed`). A guest that never
reports (its control process lost the session, a TUI that never comes up) would leave the session `agent_starting`
for ever, so the device ends it after `AGENT_START_TIMEOUT_S`, counted from the first reconcile run that saw it in
that state (in memory: a daemon restart starts the count again, so the deadline is extended at most once), and stops
its guest session (in the background). The state is checked again under the row lock, so a session the journal moved
on meanwhile is not failed. Sessions whose start or stop task still runs are left to that task.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sessions import records
from agent_runner.sessions.events import fail
from agent_runner.sessions.service import ATTRIBUTE as SERVICE_ATTRIBUTE
from agent_runner.sessions.service import SessionService
from agent_runner.sessions.stop import stop_guest

AGENT_START_TIMEOUT_S = 120.0
REASON = "agent_start_failed"
SEEN = "_agent_starting_seen"


def _seen(context: Any) -> dict[str, float]:
    found = getattr(context, SEEN, None)
    if isinstance(found, dict):
        return found
    created: dict[str, float] = {}
    setattr(context, SEEN, created)
    return created


async def agent_start_timeout(run: Run) -> None:
    service = getattr(run.context, SERVICE_ATTRIBUTE, None)
    if not isinstance(service, SessionService):
        return
    env = service.env
    seen = _seen(run.context)
    now = env.clock()
    async with env.transaction() as conn:
        listed = await records.listed(conn)
    starting = {session.id for session in listed if session.state == "agent_starting"} - set(service.tasks)
    for gone in [sid for sid in seen if sid not in starting]:
        del seen[gone]
    for sid in sorted(starting):
        first = seen.setdefault(sid, now)
        if now - first < AGENT_START_TIMEOUT_S:
            continue
        del seen[sid]
        async with env.transaction() as conn:
            # the journal may have moved the session on since the list was read: decide under the row lock
            cur = await conn.execute("SELECT state FROM runner.session WHERE id = %s FOR UPDATE", (sid,))
            row = await cur.fetchone()
            moved = None
            if row is not None and row[0] == "agent_starting":
                moved = await fail(conn, env.outbox, sid, REASON, at=env.at())
        if moved is not None:
            env.line(f"agent_start_timeout session_id={sid}")
            _stop_later(run.context, env, sid)


STOPS = "_agent_start_timeout_stops"


def _stop_later(context: Any, env: Any, session_id: str) -> None:
    """Stop the guest session in the background: the reconcile run never waits for a VM (`sessions.service`)."""
    tasks = getattr(context, STOPS, None)
    if not isinstance(tasks, set):
        tasks = set()
        setattr(context, STOPS, tasks)
    task = asyncio.ensure_future(stop_guest(env, session_id))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


STEPS = [ReconcileStep(56, "agent_start_timeout", agent_start_timeout)]
