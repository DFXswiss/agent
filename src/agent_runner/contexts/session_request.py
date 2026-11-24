"""Request amender of this area (`sessions.extensions`, `<area>/session_request.py:AMENDERS`): restore of the
original conversation or the reported emergency path (`restore.Restorer.amend`, ADR-072 section 7). It replaces
RUN-05's `prompts.restart` amender."""

from __future__ import annotations

from typing import Any

from agent_runner.contexts.env import contexts_for
from agent_runner.contexts.restore import Restorer
from agent_runner.sessions.extensions import RequestAmender
from agent_runner.sessions.records import LocalSession


async def amend(context: Any, session: LocalSession, request: dict[str, Any]) -> None:
    env = contexts_for(context)
    if env is not None:
        await Restorer(env).amend(session, request)


AMENDERS = [RequestAmender("contexts.restore", amend)]
