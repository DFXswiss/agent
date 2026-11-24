"""Bounds of the display events a guest writes (ADR-086 section 3, ADR-057; 05-runner.md sections 5.2, 6.4 and 7).

Every kind a busy session can produce in masses is bundled or summarised at the source; the content itself stays
complete (the terminal in ``terminal.cast``, tools and outputs in the transcript, files in the workspace). The bounds
are settings with generous defaults: each consumer takes its dataclass as a constructor argument (tests pass small
values), the defaults below are what a guest runs with.

* ``TerminalLimits`` (``recorder.py``): a ``session.terminal`` record is written once output has been quiet for
  ``quiet_ms`` (100 ms: typing echoes and prompts appear at once); under sustained output a record grows until it is
  ``max_age_ms`` old (1 s) or its **encoded** body (the JSON the runner encrypts, escapes counted) reaches
  ``record_json`` bytes (60 KiB, below the 64 KiB ciphertext bound of E8). The same bytes in far fewer events.
* ``HookLimits`` (``hook_summary.py``): ``allow``/``none`` records of ``PreToolUse``/``PostToolUse`` are counted per
  session and written as one ``session.hook`` summary at most every ``summary_seconds`` (10 s), before any other
  hook or state record of the session; 0 turns the summaries off.
* ``FileLimits`` (``scanner.py``, HOT-06, ADR-057): more than ``bulk_files`` changes in one scan pass become one
  ``op: "summary"`` record; at most ``window_files`` records or ``window_bytes`` of patches per ``window_seconds``.

``KIND_BOUNDS`` names the bound of every journal kind (``journal.KINDS``); the event-bounds test fails for a kind
without one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

KIB = 1024


@dataclass(frozen=True)
class TerminalLimits:
    """Bounds of the ``session.terminal`` records of one session (module docstring)."""

    quiet_ms: int = 100
    max_age_ms: int = 1000
    record_json: int = 60 * KIB


@dataclass(frozen=True)
class HookLimits:
    """Bounds of the ``session.hook`` records of one session (module docstring)."""

    summary_seconds: float = 10.0


@dataclass(frozen=True)
class FileLimits:
    """Bounds of the ``session.file`` records of one session (``scanner.py`` module docstring)."""

    bulk_files: int = 100
    window_seconds: float = 600.0
    window_files: int = 1000
    window_bytes: int = 8 * 1024 * 1024
    summary_paths: int = 20


TERMINAL = TerminalLimits()
HOOKS = HookLimits()
FILES = FileLimits()

KIND_BOUNDS: Mapping[str, str] = {
    "session.state": "one per state change of the session (bounded by its turns)",
    "session.output": "one per assistant message of the main transcript (bounded by the model); text <= 64 Ki chars",
    "session.tool": "one per completed tool call of the main transcript (bounded by the model); fields <= 64 Ki chars",
    "session.hook": "allow/none of PreToolUse/PostToolUse: one summary per HookLimits.summary_seconds; others single",
    "session.tools_ready": "one per agent run (its first tools/list, toolsready.py): bounded by starts and resumes",
    "session.file": "FileLimits: bulk passes summarised, window of files and bytes per session",
    "session.terminal": "TerminalLimits: quiet 100 ms, at most one record per second under sustained output or 60 KiB",
    "usage": "one per model request (<= 32 in flight); the runner merges a session's deltas over 10 s",
    "operation.request": "one per request_operation call (MCP wait per call; hub limits per operation type)",
    "platform.query": "one per platform query (MCP read bucket: burst 120, 2/s per session)",
    "ping.delivered": "one per delivered ping batch (bounded by the pings the hub sends)",
    "platform.read": "one per MCP read (bucket: burst 120, 2/s per session)",
    "platform.refused": "first refusal of a stretch only (bucket: burst 120, 2/s per session)",
    "metrics": "one sample round per runner request (every 15 s)",
    "workvm.call": "one per work-VM MCP tool call (the guest waits for each call's answer, up to 33 s)",
    "guest.error": "never an event: the runner writes a log line only (auth failures <= 10 per 60 s per UID)",
}
