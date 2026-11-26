"""Request amender of the modes area (`sessions.extensions`, `<area>/session_request.py:AMENDERS`; ADR-098 sections 1
and 3): a session of a task with working modes learns them at its start.

One line of the harness context for every guest: the modes that are on for the task, each with the section of the
rules that is its brief (`images/harness/RULES.md`, "Working mode: <name>"). The line is a fact of the session: a
resumed conversation that started under other modes gets it as a session notice (`agent_guest.harness.notice`), so the
brief of a mode that was switched off is gone from the next turn and a new one is named. Without a mode nothing is
added - a task without a mode works exactly as before (ADR-098 section 2). The rules list the catalogue and how to ask
for a mode in every session; this line only says which ones are on.
"""

from __future__ import annotations

from typing import Any

from agent_runner.modes import state
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions.extensions import RequestAmender
from agent_runner.sessions.records import LocalSession


def harness_line(modes: list[str]) -> str:
    sections = ", ".join(f'"Working mode: {name}"' for name in modes)
    return (
        f"Working modes of this task: {', '.join(modes)}. Follow the sections {sections} of the rules for them;"
        " a person switches a mode off in the task page."
    )


async def amend(context: Any, session: LocalSession, request: dict[str, Any]) -> None:
    async with store_session(context.pool) as conn:
        modes = await state.active(conn, session.task_id)
    if not modes:
        return
    harness = request["harness"]
    harness["context_markdown"] = str(harness["context_markdown"]).rstrip("\n") + "\n" + harness_line(modes)


AMENDERS = [RequestAmender("modes.brief", amend)]
