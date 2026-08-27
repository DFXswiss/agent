"""Is the Grok TUI in this tmux pane currently working?

The Grok CLI paints that state itself. Pane flicker, process lists, and
log mtimes are not this question.
"""

from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
WORKING_RE = re.compile(
    r"Thinking…|Thinking\.\.\.|Waiting for response|"
    r"\[stop\]|Esc:cancel|Preparing [A-Za-z0-9_]+|"
    r"1/3:select|don't ask again for anything",
    re.IGNORECASE,
)
PERMISSION_RE = re.compile(r"1/3:select", re.IGNORECASE)
PERMISSION_HINT = "Tab:next option"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def grok_pane_is_working(pane: str) -> bool:
    """True only when the TUI shows an in-flight turn (thinking, tool, stop)."""
    plain = strip_ansi(pane)
    return WORKING_RE.search(plain) is not None


def grok_permission_prompt(pane: str) -> bool:
    """True when Grok is blocked on a tool-approval modal."""
    plain = strip_ansi(pane)
    if PERMISSION_RE.search(plain) is None:
        return False
    return PERMISSION_HINT.lower() in plain.lower()
