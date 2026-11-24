"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`, ADR-032; ADR-059 decision 8,
ADR-080 section 4). Runs in the journal page's transaction, so the work itself runs in the background:

- a session in a final state: its session work VM is torn down, and the task work VM when no other session of the
  task is active; its test runs end `cancelled` (`session_ended`);
- a session that was suspended (paused): the VMs it works in are stopped (`WorkVms.pause`, no RAM and no runtime
  while they stand) unless a job still runs in them - a long build the session waits for keeps running and the idle
  pass decides as before. The credit stop is the reconcile step `workvm`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import FINAL, LocalSession
from agent_runner.workvm.reconcile import closed_tasks
from agent_runner.workvm.service import existing, runs_of

PENDING: set[asyncio.Task[Any]] = set()


def _background(work: Awaitable[Any]) -> None:
    task = asyncio.ensure_future(work)
    PENDING.add(task)
    task.add_done_callback(PENDING.discard)


async def observe(journal: Any, _conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    workvms = existing(journal.env.context)
    if workvms is None:
        return
    state = body.get("state")
    if state in FINAL:
        # the stop of a closed task's session may arrive before the reconcile step saw the closed task
        closed = session.task_id in closed_tasks(getattr(journal.env, "desired", None) or {})
        reason = "task_closed" if closed else "session_ended"
        runs_of(journal.env.context, workvms).cancel(reason=reason, session_id=session.id)
        _background(workvms.session_ended(session, reason=reason))
    elif state == "suspended":
        _background(workvms.pause(session, reason="session_paused", when_idle=True))


OBSERVERS = [StateObserver("workvm.session_end", observe)]
