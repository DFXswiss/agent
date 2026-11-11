"""Shared terminal-text sanitization before persistence or display."""

from __future__ import annotations

import re


# String controls must be matched before the shorter generic ESC form. Both
# 7-bit and 8-bit string controls accept either ST spelling; an unterminated
# string consumes the remaining text rather than exposing its payload.
_TERMINAL_ESCAPE_RE = re.compile(
    r"(?:"
    r"\x1b\][\s\S]*?(?:\x07|\x9c|\x1b\\|\Z)"  # OSC, 7-bit
    r"|\x9d[\s\S]*?(?:\x07|\x9c|\x1b\\|\Z)"  # OSC, 8-bit
    r"|\x1b[P^_X][\s\S]*?(?:\x9c|\x1b\\|\Z)"  # DCS/PM/APC/SOS, 7-bit
    r"|[\x90\x98\x9e\x9f][\s\S]*?(?:\x9c|\x1b\\|\Z)"  # 8-bit strings
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[ -/]*[0-~]"  # other complete ESC sequences
    r")"
)
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Before ordinary terminal sanitization, a known secret needs a stricter path:
# a CSI final byte may itself be the next secret character. The first two
# alternatives therefore also accept a sequence prefix whose final byte is
# supplied by the following literal secret character.
_TERMINAL_INTERLEAVE = (
    r"(?:"
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*"
    r"|\x1b[ -/]*"
    rf"|{_TERMINAL_ESCAPE_RE.pattern}"
    r"|[\x00-\x1f\x7f-\x9f]"
    r")*"
)


def redact_terminal_interleaved_secret(
    text: str, secret: str, *, replacement: str = "[redacted]"
) -> str:
    """Redact one exact secret even when terminal controls split its characters."""
    if not secret:
        return text
    pattern = re.compile(
        _TERMINAL_INTERLEAVE.join(re.escape(char) for char in secret)
    )
    return pattern.sub(replacement, text)


def sanitize_terminal_text(text: str) -> str:
    """Remove terminal escapes and controls, preserving only tab and newline."""
    without_escapes = _TERMINAL_ESCAPE_RE.sub("", text or "")
    return _TERMINAL_CONTROL_RE.sub("", without_escapes)
