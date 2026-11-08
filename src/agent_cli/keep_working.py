"""Keep a Grok session working until the standing assignment is done.

The TUI stops after each turn. That is not rest. One standing instruction
is sent the first time the pane is idle; later idle ticks send a short
continue. In-flight turns are never interrupted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .grok_pane import grok_pane_is_idle, grok_pane_is_working, grok_permission_prompt
from .runtime import Runtime

STANDING = (
    "Continue the standing assignment in this working directory until it is "
    "fully complete. Do not stop after one file change, a plan, or a status "
    "paragraph. Stop only when the assignment is done or you are blocked."
)
CONTINUE = "Continue."


def tick(
    runtime: Runtime,
    session_id: str,
    state: dict[str, Any],
    *,
    preflight: Callable[[], None] | None = None,
) -> str:
    """Advance one keep-working step. Mutates *state*."""
    authorize = preflight or (lambda: None)
    authorize()
    if not runtime.exists(session_id):
        return "missing"
    authorize()
    pane = runtime.capture(session_id)
    if pane.strip() == "":
        return "unobservable"
    if grok_permission_prompt(pane):
        authorize()
        runtime.input_key(session_id, "enter")
        return "approved"
    if grok_pane_is_working(pane) or not grok_pane_is_idle(pane):
        return "working"
    if not state.get("standing_sent"):
        authorize()
        runtime.input_text(session_id, STANDING)
        authorize()
        runtime.input_key(session_id, "enter")
        state["standing_sent"] = True
        return "standing"
    authorize()
    runtime.input_text(session_id, CONTINUE)
    authorize()
    runtime.input_key(session_id, "enter")
    return "continue"
