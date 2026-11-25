"""Rules of controlled subagents (05-runner.md sections 4.3 and 13, 03-api.md sections 9 and 12, ADR-088).

The subagent limits are settings of the hub (`limits.py`, ADR-088 section 2); this module keeps none of them.

A child gets the resource limits of a main session (05 section 4.3), with `RuntimeMaxSec` = the runtime setting when
one is set (`child_limits`), the initial prompt `"Purpose: <purpose>\\n\\n<prompt>"` and a harness line naming its
parent and task (05 section 13).
"""

from __future__ import annotations

from dataclasses import replace

from agent_runner.sessions.harness import MAIN_LIMITS, Limits, inline
from agent_runner.subagents.limits import SubagentLimits


def child_limits(found: SubagentLimits | None) -> Limits:
    """The resource limits of a child: those of a main session, the runtime setting as `RuntimeMaxSec` when set."""
    if found is None or not found.max_runtime_seconds:
        return MAIN_LIMITS
    return replace(MAIN_LIMITS, runtime_max_sec=found.max_runtime_seconds)


FINAL_TEXT_MAX_BYTES = 64 * 1024
"""`final_text` is at most 64 KiB (UTF-8 bytes, hence also at most 65536 characters, the schema's `maxLength`)."""


def initial_prompt(purpose: str, prompt: str) -> str:
    return f"Purpose: {purpose}\n\n{prompt}"


def harness_line(parent_session_id: str, task_id: str, *, current: bool = True) -> str:
    """The harness addition of a subagent session (05 section 13), one line of the context block. A child on a guest
    that knows ADR-088 (`current`) answers each instruction of its main agent and keeps running until that agent stops
    it; a child on an older guest keeps the former line (one-shot, ADR-038)."""
    if not current:
        return (
            f"- You are a subagent session of session `{inline(parent_session_id)}` for task `{inline(task_id)}`;"
            " finish by answering; you may request further subagents only via `subagent.request`."
        )
    return (
        f"- You are a subagent session of session `{inline(parent_session_id)}` for task `{inline(task_id)}`; answer"
        " each instruction of your main agent with your last message of that turn; you keep running and receive"
        " further instructions until your main agent stops you; you may request further subagents only via"
        " `subagent.request`."
    )


def cut_text(text: str) -> str:
    """`text` cut to `FINAL_TEXT_MAX_BYTES` UTF-8 bytes at a character boundary."""
    data = text.encode()
    if len(data) <= FINAL_TEXT_MAX_BYTES:
        return text
    return data[:FINAL_TEXT_MAX_BYTES].decode(errors="ignore")
