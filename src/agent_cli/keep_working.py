"""Keep a Grok session working until the standing assignment is done.

The TUI stops after each turn. That is not rest. One standing instruction
is sent the first time the pane is idle; later idle ticks send a short
continue. In-flight turns are never interrupted.
"""

from __future__ import annotations

from typing import Any

from .grok_pane import grok_pane_is_working, grok_permission_prompt
from .runtime import Runtime

STANDING = (
    "Continue the standing assignment in this working directory until it is "
    "fully complete. Do not stop after one file change, a plan, or a status "
    "paragraph. Stop only when the assignment is done or you are blocked."
)
CONTINUE = "Continue."


def tick(runtime: Runtime, session_id: str, state: dict[str, Any]) -> str:
    """Advance one keep-working step. Mutates *state*."""
    if not runtime.exists(session_id):
        return "missing"
    pane = runtime.capture(session_id)
    if grok_permission_prompt(pane):
        runtime.input_key(session_id, "enter")
        return "approved"
    if grok_pane_is_working(pane):
        return "working"
    if not state.get("standing_sent"):
        runtime.input_text(session_id, STANDING)
        runtime.input_key(session_id, "enter")
        state["standing_sent"] = True
        return "standing"
    runtime.input_text(session_id, CONTINUE)
    runtime.input_key(session_id, "enter")
    return "continue"
