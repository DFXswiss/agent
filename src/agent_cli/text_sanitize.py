"""Shared terminal-text sanitization before persistence or display."""

from __future__ import annotations

import re


# String controls must be matched before the shorter generic ESC form.  Only
# complete strings are removed here; any remaining introducer is removed with
# the C0/C1 controls below without consuming ordinary printable text.
_TERMINAL_ESCAPE_RE = re.compile(
    r"(?:"
    r"\x1b\][\s\S]*?(?:\x07|\x9c|\x1b\\)"  # OSC, 7-bit
    r"|\x9d[\s\S]*?(?:\x07|\x9c|\x1b\\)"  # OSC, 8-bit
    r"|\x1b[P^_X][\s\S]*?\x1b\\"  # DCS/PM/APC/SOS, 7-bit
    r"|[\x90\x98\x9e\x9f][\s\S]*?(?:\x9c|\x1b\\)"  # 8-bit strings
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[0-~]"  # other complete ESC sequences
    r")"
)
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal escapes and controls, preserving only tab and newline."""
    without_escapes = _TERMINAL_ESCAPE_RE.sub("", text or "")
    return _TERMINAL_CONTROL_RE.sub("", without_escapes)
