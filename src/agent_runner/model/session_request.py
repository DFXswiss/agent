"""Request amender of the model area (`sessions.extensions`, `<area>/session_request.py:AMENDERS`, ADR-032): a new main
session that starts while it waits for model quota is paused before its first prompt (KOR-50, ADR-081 section 3).

`model.access.resolve` started the session on the access with the earliest release and recorded its wait
(`model.access.hold`). Its first prompt is therefore taken off the start request (the agent starts without one) and its
delivery row goes back from `registered` to `pending`: the prompt stays with the runner until the wait ends and is
then delivered like a follow-up (`deliver.Deliverer`, deferred meanwhile by `billing.pause.deferred`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runner.model import pauses
from agent_runner.orgs.db import session as store_session
from agent_runner.prompts import queue
from agent_runner.sessions.extensions import RequestAmender
from agent_runner.sessions.records import LocalSession


async def amend(context: Any, session: LocalSession, request: dict[str, Any]) -> None:
    if session.id not in pauses.holding(context):
        return
    initial = request.pop("initial_prompt", None)
    if not isinstance(initial, Mapping):
        return
    async with store_session(context.pool) as conn:
        await queue.set_state(conn, str(initial["prompt_id"]), "pending", expected=("registered",))
    context.log.write(f"runner.model first_prompt_held session_id={session.id}\n")
    context.log.flush()


AMENDERS = [RequestAmender("model.wait", amend)]
