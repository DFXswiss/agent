"""The first prompt of a new main session (05-runner.md section 6.1 step 5, ADR-032 `FIRST_PROMPT`).

`first_prompt(context, session)` is called by the session start before `POST /v1/sessions`: the head prompt the hub
routed to this session (`pending_prompts`, applied here first so the start never waits for reconcile step 60),
verified and decrypted into memory, recorded `registered` with channel `start`; the guest registers its hash and puts
it on the agent's command line. None when no prompt is routed yet: the session then waits in `prompt_delivering`
and the delivery sends it as soon as it is routed (`deliver.Deliverer`).
"""

from __future__ import annotations

from typing import Any

from agent_runner.prompts.deliver import deliverer_for
from agent_runner.sessions.extensions import InitialPrompt
from agent_runner.sessions.records import LocalSession


async def first_prompt(context: Any, session: LocalSession) -> InitialPrompt | None:
    deliverer = deliverer_for(context)
    return None if deliverer is None else await deliverer.first_prompt(session)
