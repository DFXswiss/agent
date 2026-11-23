"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`, ADR-032; 05-runner.md section
13): every record of a child session (`depth` > 0) tells the controller its state, so the first `idle` (the end of its
only turn) and its end are handled at once instead of at the next tick. Runs in the journal page's transaction and
only touches memory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import LocalSession
from agent_runner.subagents.controller import existing


async def observe(journal: Any, _conn: AsyncConnection[Any], session: LocalSession, _body: Mapping[str, Any]) -> None:
    controller = existing(journal.env.context)
    if controller is not None and session.depth > 0:
        controller.observed(session)


OBSERVERS = [StateObserver("subagents.children", observe)]
