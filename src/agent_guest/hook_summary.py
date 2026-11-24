"""Summaries of the routine hook records of a session (ADR-086 section 3; 03-api.md section 7.2; 05-runner.md 5.3).

Every hook call used to become one ``session.hook`` record, i.e. one event and one audit row at the hub: at least
two per tool call, also for the tool calls of Claude's internal subagents. The routine ones - ``PreToolUse`` with
``allow`` and ``PostToolUse`` with ``none``, without a ``reason`` - are counted per session instead and written as
one record

    ``{event: "summary", decision: "allow", counts: {<event>: n}, tools: {<tool name>: n}, from, to}``

``counts`` per hook event, ``tools`` the ``PreToolUse`` calls per tool name (the allowed tool calls; a call without
a valid name counts only in ``counts``), ``from``/``to`` the journal times of the first and the last counted record.

A summary is written (``HookSummaries`` inside the journal's insert, ``journal.py``):

* when a counted record finds the session's first counted record ``HookLimits.summary_seconds`` (10 s) old, or its
  ``tools`` map at ``MAX_TOOLS`` names - the record is counted first;
* before any other ``session.hook`` or ``session.state`` record of the session (a ``Stop``, a deny, a block, a fault,
  ``UserPromptSubmit``, the session's end) - so those keep their place after the calls they follow and stay single
  (they feed ``hook.denied``, the hook metrics and the subagent results);
* by ``flush_due`` for a session whose first counted record is that old (the control process calls it every second,
  ``control_routes/hook_summary.py``).

``summary_seconds`` 0 turns the summaries off: every call is a record of its own, as before (``HookSummaries.enabled``).

The counts live in memory and follow the journal's transactions: ``begin``/``commit``/``rollback`` keep them in step
with what the database holds (a rolled-back transaction neither counts nor resets). A restart of the control process
loses at most the counts of the last 10 s; the calls themselves are in the transcript (``session.tool``).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_guest.clock import iso_ms
from agent_guest.limits import HOOKS, HookLimits

KIND = "session.hook"
COUNTED = {"PreToolUse": "allow", "PostToolUse": "none"}
"""Hook event -> the routine decision that is counted instead of journaled."""
COUNTED_KEYS = frozenset({"event", "decision", "tool", "rule", "path"})
FLUSHING_KINDS = frozenset({KIND, "session.state"})
TOOL_EVENT = "PreToolUse"
MAX_TOOLS = 256
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass
class Pending:
    """The counted records of one session since its last summary."""

    first: float
    last: float
    counts: dict[str, int] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {
            "event": "summary",
            "decision": "allow",
            "counts": dict(self.counts),
            "tools": dict(self.tools),
            "from": iso_ms(self.first),
            "to": iso_ms(self.last),
        }


def counted(kind: str, body: Mapping[str, Any]) -> bool:
    """A routine hook record (module docstring)."""
    event = body.get("event")
    return (
        kind == KIND
        and isinstance(event, str)
        and COUNTED.get(event) == body.get("decision")
        and set(body) <= COUNTED_KEYS
    )


class HookSummaries:
    """Per-session counts of routine hook records; every method runs under the journal's lock."""

    def __init__(self, limits: HookLimits = HOOKS) -> None:
        self.limits = limits
        self._pending: dict[str, Pending] = {}
        self._saved: dict[str, Pending] | None = None

    @property
    def enabled(self) -> bool:
        return self.limits.summary_seconds > 0

    # -- transactions of the journal ---------------------------------------------------------------------------------

    def begin(self) -> None:
        self._saved = None

    def commit(self) -> None:
        self._saved = None

    def rollback(self) -> None:
        if self._saved is not None:
            self._pending = self._saved
        self._saved = None

    def _change(self) -> None:
        if self._saved is None:
            self._saved = copy.deepcopy(self._pending)

    # -- counting ----------------------------------------------------------------------------------------------------

    def count(self, session_id: str, body: Mapping[str, Any], now: float) -> dict[str, Any] | None:
        """Count a routine record; returns the summary to write now (window over or ``tools`` full), else None."""
        self._change()
        pending = self._pending.setdefault(session_id, Pending(first=now, last=now))
        pending.last = max(pending.last, now)
        event = str(body["event"])
        pending.counts[event] = pending.counts.get(event, 0) + 1
        tool = body.get("tool")
        if event == TOOL_EVENT and isinstance(tool, str) and _TOOL_RE.fullmatch(tool):
            pending.tools[tool] = pending.tools.get(tool, 0) + 1
        if now - pending.first >= self.limits.summary_seconds or len(pending.tools) >= MAX_TOOLS:
            return self.take(session_id)
        return None

    def take(self, session_id: str) -> dict[str, Any] | None:
        """The summary of the session's counted records (they are reset), None without any."""
        if session_id not in self._pending:
            return None
        self._change()
        return self._pending.pop(session_id).body()

    def due(self, now: float) -> list[str]:
        """The sessions whose first counted record is ``summary_seconds`` old, in counting order."""
        return [sid for sid, pending in self._pending.items() if now - pending.first >= self.limits.summary_seconds]

    @property
    def sessions(self) -> list[str]:
        return list(self._pending)
