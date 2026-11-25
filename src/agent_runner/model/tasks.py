"""Daemon tasks of the model area.

`model_fallback` (KOR-50, ADR-081 section 4): every `fallback.TICK_S` the run of the reconcile step of the same name
(`model.fallback.run_once`), so a pause ends within seconds of its release time and a used-up access is acted on
without waiting for the next desired state. It does nothing while the device is not enrolled (no deliverer); a failing
round is logged and the loop goes on.

`operator_subscriptions` (KOR-23, ADR-052; 05-runner.md section 10.2): every `TICK_S` the sessions
bound to a token file of the operator's pool are renewed (`model.gateways.refresh`), so a file replaced from outside
(a renewed short-lived token) reaches the session's gateway within seconds, and a session whose file was blocked
moves to another one. A renewal with an unchanged file changes nothing (the stamp compares the file's identity; the
file is not read again). The task ends at once when the pool is not configured. A failing round is logged and the loop
goes on.

Daemon task `usage_merge` (KOR-35): every second the merged usage items that are due go to the outbox
(`model.merge.run`).
"""

from __future__ import annotations

from typing import Any

from agent_runner.model import fallback, merge, operator
from agent_runner.model.gateways import RENEWED, live_sessions, refresh, running_service
from agent_runner.prompts.deliver import deliverer_for
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
    if operator.configured(getattr(context, "config", None)) is None and getattr(context, "identity", None) is None:
        return
    while not context.stopping.is_set():
        try:
            await tick(context)
        except Exception as exc:
            _line(context, f"operator_tick_failed error={type(exc).__name__}")
        await context.host.sleep(TICK_S)


async def run_fallback(context: Any) -> None:
    while not context.stopping.is_set():
        try:
            deliverer = deliverer_for(context)
            if deliverer is not None:
                await fallback.run_once(deliverer)
        except Exception as exc:
            _line(context, f"model_fallback_tick_failed error={type(exc).__name__}")
        await context.host.sleep(fallback.TICK_S)


TASKS = [
    TaskSpec("operator_subscriptions", run),
    TaskSpec("model_fallback", run_fallback),
    TaskSpec("usage_merge", merge.run),
]
