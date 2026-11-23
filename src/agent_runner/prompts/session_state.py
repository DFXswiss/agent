"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`, ADR-032; 05-runner.md sections
6.1, 6.2, 5.4; 03-api.md section 7.2).

Runs in the journal page's transaction after the session area wrote the transition:

- `agent_starting`: the initial prompt handed over with the start (channel `start`) is held by the guest -> `typed`;
- `prompt_accepted {prompt_id}`: the row of this session -> `accepted`, `last_prompt_at`;
- `prompt_refused {prompt_id, reason: "delivery_failed"}` for an open row of this session: `failed` and a
  `session.status` event `{state, reason: "delivery_failed", details: {prompt_event_id}}` for the chat; a session that
  never accepted a prompt (still in the start states) fails `delivery_failed` and its guest session is stopped. A
  refusal of text nobody registered (`unregistered`) is audited by the session area and changes no row here.

Every record also tells the delivery the session's state (idle times, a wake-up for the next prompt).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from agent_runner.prompts import deliver, queue
from agent_runner.sessions import records
from agent_runner.sessions.events import fail, session_event
from agent_runner.sessions.extensions import StateObserver
from agent_runner.sessions.records import LocalSession


async def observe(journal: Any, conn: AsyncConnection[Any], session: LocalSession, body: Mapping[str, Any]) -> None:
    state, prompt_id = body.get("state"), body.get("prompt_id")
    env = journal.env
    if state == "agent_starting":
        await queue.typed_start(conn, session.id)
    elif state == "prompt_accepted" and isinstance(prompt_id, str):
        if await queue.accepted_prompt(conn, session.id, prompt_id):
            await records.update(conn, session.id, last_prompt_at=datetime.fromtimestamp(env.wall(), UTC))
    elif state == "prompt_refused" and body.get("reason") == "delivery_failed" and isinstance(prompt_id, str):
        await _failed(journal, conn, session, prompt_id)
    found = deliver.existing(env.context)
    if found is not None:
        current = await records.get(conn, session.id)
        found.observe(session.id, session.state if current is None else current.state)


async def _failed(journal: Any, conn: AsyncConnection[Any], session: LocalSession, prompt_id: str) -> None:
    row = await queue.delivery(conn, prompt_id, lock=True)
    if row is None or row.session_id != session.id or row.state not in queue.OPEN:
        return
    env = journal.env
    await queue.set_state(conn, prompt_id, "failed")
    at = str(env.at())
    if session.state in records.START_CHAIN:
        await fail(conn, env.outbox, session.id, "delivery_failed", at=at)
        found = deliver.existing(env.context)
        if found is not None:
            found.stop_later(session.id)
        return
    status = {"state": session.state, "reason": "delivery_failed", "details": {"prompt_event_id": prompt_id}}
    await session_event(conn, env.outbox, session, kind="session.status", body=status, ts=at)


OBSERVERS = [StateObserver("prompts.delivery", observe)]
