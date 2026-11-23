"""Request amender of this area (`sessions.extensions`, `<area>/session_request.py:AMENDERS`): the harness hint of a
restarted main session (`resume.restart_hint`, 03-api.md section 4.6.3)."""

from __future__ import annotations

from typing import Any

from agent_runner.prompts.deliver import deliverer_for
from agent_runner.prompts.resume import restart_hint
from agent_runner.sessions.extensions import RequestAmender
from agent_runner.sessions.records import LocalSession


async def amend(context: Any, session: LocalSession, request: dict[str, Any]) -> None:
    deliverer = deliverer_for(context)
    if deliverer is not None:
        await restart_hint(deliverer.env, session, request)


AMENDERS = [RequestAmender("prompts.restart", amend)]
