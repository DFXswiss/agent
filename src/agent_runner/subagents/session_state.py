"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`, ADR-032; 05-runner.md section
13, ADR-088 section 1): every record of a child session (`depth` > 0) tells the controller its state, so the end of a
turn (`idle`) and the child's end are handled at once instead of at the next tick. In the journal page's transaction
it also moves the child's instructions (`instructions.py`): `prompt_accepted {prompt_id}` -> that instruction's turn
began, `idle` -> the turn of every accepted instruction ended."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import LocalSession
from agent_runner.subagents import instructions
from agent_runner.subagents.controller import existing

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


async def observe(journal: Any, conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    if session.depth == 0:
        return
    state, prompt_id = body.get("state"), body.get("prompt_id")
    at = datetime.fromtimestamp(journal.env.wall(), UTC)
    if state == "prompt_accepted" and isinstance(prompt_id, str) and _UUID_RE.fullmatch(prompt_id):
        await instructions.accepted(conn, session.id, prompt_id, at)
    elif state == "idle":
        await instructions.turn_ended(conn, session.id, at)
    controller = existing(journal.env.context)
    if controller is not None:
        controller.observed(session)


OBSERVERS = [StateObserver("subagents.children", observe)]
