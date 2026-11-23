"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`, ADR-032; ADR-059 decision 8):
when a session reaches a final state its session work VM is torn down, and the task work VM when no other session of
the task is active. Runs in the journal page's transaction, so the teardown itself runs in the background."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import FINAL, LocalSession
from agent_runner.workvm.service import existing

PENDING: set[asyncio.Task[None]] = set()


async def observe(journal: Any, _conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    workvms = existing(journal.env.context)
    if workvms is None or body.get("state") not in FINAL:
        return
    task = asyncio.ensure_future(workvms.session_ended(session))
    PENDING.add(task)
    task.add_done_callback(PENDING.discard)


OBSERVERS = [StateObserver("workvm.session_end", observe)]
