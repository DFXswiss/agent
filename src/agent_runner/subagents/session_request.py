"""Request amender of this area (`sessions.extensions`, `<area>/session_request.py:AMENDERS`, ADR-032): the initial
prompt, limits and harness line of a child session (`controller.Subagents.amend`, 05-runner.md section 13). A session
with `depth` > 0 that the controller did not ask for is refused, so its start fails."""

from __future__ import annotations

from typing import Any

from agent_runner.errors import RunnerError
from agent_runner.sessions.extensions import RequestAmender
from agent_runner.sessions.records import LocalSession
from agent_runner.subagents.controller import existing


async def amend(context: Any, session: LocalSession, request: dict[str, Any]) -> None:
    if session.depth == 0:
        return
    controller = existing(context)
    if controller is None:
        raise RunnerError("subagent_not_requested")
    await controller.amend(session, request)


AMENDERS = [RequestAmender("subagents.child", amend)]
