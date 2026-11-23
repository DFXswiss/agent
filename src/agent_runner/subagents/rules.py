"""Fixed rules of controlled subagents (05-runner.md sections 4.3 and 13, 03-api.md sections 9 and 12, ADR-017).

Limits are constants in v1 (ADR-017): depth at most 2 (main session 0, children 1, grandchildren 2), at most 3 running
children per task, at most 10 children per task in total, runtime of a child at most 30 min. The hub decides them
(check H4); the runner pre-checks the same numbers (`op_prechecks.py`).

A child gets the resource limits of a main session with `RuntimeMaxSec=30min` (05 section 4.3), the initial prompt
`"Purpose: <purpose>\\n\\n<prompt>"` and a harness line naming its parent and task (05 section 13).
"""

from __future__ import annotations

from dataclasses import replace

from agent_runner.sessions.harness import MAIN_LIMITS, SUBAGENT_LIMITS, Limits, inline

MAX_DEPTH, MAX_RUNNING, MAX_TOTAL = SUBAGENT_LIMITS
RUNTIME_MAX_S = 30 * 60
CHILD_LIMITS: Limits = replace(MAIN_LIMITS, runtime_max_sec=RUNTIME_MAX_S)
FINAL_TEXT_MAX_BYTES = 64 * 1024
"""`final_text` is at most 64 KiB (UTF-8 bytes, hence also at most 65536 characters, the schema's `maxLength`)."""


def initial_prompt(purpose: str, prompt: str) -> str:
    return f"Purpose: {purpose}\n\n{prompt}"


def harness_line(parent_session_id: str, task_id: str) -> str:
    """The harness addition of a subagent session (05 section 13), one line of the context block."""
    return (
        f"- You are a subagent session of session `{inline(parent_session_id)}` for task `{inline(task_id)}`; finish"
        " by answering; you may request further subagents only via `subagent.request`."
    )


def cut_text(text: str) -> str:
    """`text` cut to `FINAL_TEXT_MAX_BYTES` UTF-8 bytes at a character boundary."""
    data = text.encode()
    if len(data) <= FINAL_TEXT_MAX_BYTES:
        return text
    return data[:FINAL_TEXT_MAX_BYTES].decode(errors="ignore")
