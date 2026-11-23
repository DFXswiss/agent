"""Daemon task `operator_subscriptions` (KOR-23, ADR-052; 05-runner.md section 10.2): every `TICK_S` the sessions
bound to a token file of the operator's pool are renewed (`model.gateways.refresh`), so a file replaced from outside
(a renewed short-lived token) reaches the session's gateway within seconds, and a session whose file was blocked
moves to another one. A renewal with an unchanged file changes nothing (the stamp compares the file's identity; the
file is not read again). The task ends at once when the pool is not configured. A failing round is logged and the loop
goes on.
"""

from __future__ import annotations

from typing import Any

from agent_runner.model import operator
from agent_runner.model.gateways import RENEWED, live_sessions, refresh, running_service
from agent_runner.registry import TaskSpec

TICK_S = 2.0


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.model {text}\n")
    context.log.flush()


async def tick(context: Any) -> None:
    service = running_service(context)
    if service is None:
        return
    env = service.env
    sessions = await live_sessions(env, set(service.tasks), RENEWED)
    for session in sessions:
        if await operator.bound(context, session.id) is None:
            continue
        try:
            await refresh(env, session)
        except Exception as exc:
            _line(context, f"operator_refresh_failed session_id={session.id} error={type(exc).__name__}")


async def run(context: Any) -> None:
    if operator.pool_for(context) is None:
        return
    while not context.stopping.is_set():
        try:
            await tick(context)
        except Exception as exc:
            _line(context, f"operator_tick_failed error={type(exc).__name__}")
        await context.host.sleep(TICK_S)


TASKS = [TaskSpec("operator_subscriptions", run)]
