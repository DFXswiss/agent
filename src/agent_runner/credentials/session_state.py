"""Observer of the guest's `session.state` records for the acting user of a turn (`sessions.extensions` `OBSERVERS`,
ADR-032; HOT-14, ADR-070): `prompt_accepted {prompt_id}` of a main session -> `acting.prompt_accepted`, of a child
session (`depth` > 0) -> `acting.instruction_accepted` (the acting user of the instruction's operation, ADR-088
section 1), `idle` -> `acting.turn_ended`. It runs in the journal page's transaction, so a turn's acting user is set
before any operation request the guest journaled after the prompt is stamped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.credentials import acting
from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import LocalSession


async def observe(journal: Any, conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    state, prompt_id = body.get("state"), body.get("prompt_id")
    if state == "prompt_accepted" and isinstance(prompt_id, str) and session.depth == 0:
        await acting.prompt_accepted(conn, session.id, prompt_id)
    elif state == "prompt_accepted" and isinstance(prompt_id, str):
        await acting.instruction_accepted(conn, session.id, prompt_id)
    elif state == "idle":
        await acting.turn_ended(conn, session.id)


OBSERVERS = [StateObserver("credentials.acting", observe)]
