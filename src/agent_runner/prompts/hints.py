"""WebSocket hints of this area (03-api.md section 6.2, ADR-016).

- `prompt {session_id, scope_id, scope_seq, via_agent?}`: a new prompt for a session of this device. The hint carries
  no content: the event is pulled and verified by the inbox, then routed (`Deliverer.hinted`). `via_agent` (ADR-083):
  the hub received the prompt from its author's agent - only the qualifier of the sender line.
- `session.control {session_id, action}`: `interrupt` or `stop` authorised by the hub (`POST /v1/sessions/{id}/control`,
  `task:write`); carried out for a session of this device (`Deliverer.control`).

A hint with fields of the wrong shape is logged and dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.prompts.deliver import CONTROL_ACTIONS, deliverer_for
from agent_runner.reconcile import wire


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.prompts {text}\n")
    context.log.flush()


async def prompt(context: Any, hint: Mapping[str, object]) -> None:
    try:
        session_id = wire.uuid_text(hint.get("session_id"), "hint")
        scope_id = wire.uuid_text(hint.get("scope_id"), "hint")
        scope_seq = wire.seq(hint.get("scope_seq"), "hint", 1)
        via_agent = hint.get("via_agent", False)
        if not isinstance(via_agent, bool):
            raise wire.ResponseInvalid("hint")
    except wire.ResponseInvalid:
        _line(context, "hint_invalid type=prompt")
        return
    deliverer = deliverer_for(context)
    if deliverer is not None:
        await deliverer.hinted(session_id, scope_id, scope_seq, via_agent=via_agent)


async def control(context: Any, hint: Mapping[str, object]) -> None:
    action = hint.get("action")
    try:
        session_id = wire.uuid_text(hint.get("session_id"), "hint")
    except wire.ResponseInvalid:
        session_id = None
    if session_id is None or not isinstance(action, str) or action not in CONTROL_ACTIONS:
        _line(context, "hint_invalid type=session.control")
        return
    deliverer = deliverer_for(context)
    if deliverer is not None:
        await deliverer.control(session_id, action)


HANDLERS: dict[str, Callable[..., Any]] = {"prompt": prompt, "session.control": control}
