"""What the login reads on the screen of `claude setup-token` (05-runner.md section 11.2, KOR-21).

The labels of the program live here and nowhere else. They were checked against Claude Code `CHECKED_VERSION`, the
pin of the golden image (`claude_code` in `images/versions.json`); a test fails when the pin moves, and the upgrade
gate (`runbooks/claude-code-upgrade-gate.md`) asks to check the login against the new version before this constant
follows. Every function works on the rows of `screen.Screen` (real spaces, no control sequences), never on raw output.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

CHECKED_VERSION = "2.1.330"
"""The Claude Code version whose screens the labels below were taken from."""
PROMPT_LABEL = "Paste code here if prompted"
"""Shown above the input field; a code typed before it appears is lost."""
ERROR_LABEL = "OAuth error"
"""`✘ OAuth error: That code wasn't accepted. …` and `Press Enter to retry`: the program waits, it does not end."""
TOKEN_MARKER = "OAuth token"
"""`Your OAuth token (valid for 1 year):`, followed by an empty row and the token on its own rows."""
TOKEN_PREFIX = "sk-" + "ant-oat"
TOKEN_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[0-9]{2}-[A-Za-z0-9_-]{16,4000}")
"""A long-lived setup token (`sk-ant-oatNN-…`, at most 4096 characters as the credential contract allows)."""
TOKEN_PIECE_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]*|[A-Za-z0-9_-]{16,}")
"""A run of rows that may be a token or a piece of one: token characters only, beginning with the prefix or at least
16 long (a lone word of the UI is shorter)."""


def _rows(lines: Sequence[str]) -> list[str]:
    return [line.strip() for line in lines]


def blocks(lines: Sequence[str]) -> list[tuple[int, list[str]]]:
    """The runs of non-empty rows (stripped) with the index of their first row."""
    found: list[tuple[int, list[str]]] = []
    current: list[str] = []
    for index, row in enumerate(_rows(lines)):
        if row:
            if not current:
                found.append((index, current))
            current.append(row)
        else:
            current = []
    return found


def _token_blocks(lines: Sequence[str]) -> list[str]:
    return [joined for _, rows in blocks(lines) if TOKEN_RE.fullmatch(joined := "".join(rows))]


def _token_char_runs(lines: Sequence[str]) -> int:
    return sum(1 for _, rows in blocks(lines) if TOKEN_PIECE_RE.fullmatch("".join(rows)))


def shown(lines: Sequence[str], label: str) -> bool:
    return any(label in row for row in lines)


def parse_token(lines: Sequence[str]) -> str | None:
    """The token on the screen, None when there is none.

    With the marker: the first run of non-empty rows after the marker row, joined (a narrow window wraps the token
    over several rows), must be a whole token. Without the marker (a later version may word it differently): exactly
    one run of rows on the screen that is, joined, a whole token and nothing else. A run with any other text, a second
    candidate or a marker followed by something else yields None. In both ways the token must also be the only run on
    the screen made of token characters alone: a token split by an empty row (a piece left on its own run) yields None,
    so a part of a token is not taken for the whole."""
    rows = _rows(lines)
    marker = next((i for i, row in enumerate(rows) if TOKEN_MARKER in row), None)
    if marker is None:
        candidates = _token_blocks(rows)
        return candidates[0] if len(candidates) == 1 and _token_char_runs(rows) == 1 else None
    if _token_char_runs([*rows[:marker], "", *rows[marker + 1 :]]) != 1:
        return None
    after = blocks(rows[marker + 1 :])
    token = "".join(after[0][1]) if after else ""
    return token if TOKEN_RE.fullmatch(token) else None


def shape(lines: Sequence[str]) -> str:
    """The form of a screen for the runner log: counts and flags only, never its text (no URL, code or token)."""
    rows = _rows(lines)

    def flag(label: str) -> str:
        return "yes" if shown(rows, label) else "no"

    return (
        f"rows={sum(1 for row in rows if row)} marker={flag(TOKEN_MARKER)} token_blocks={len(_token_blocks(rows))} "
        f"prompt={flag(PROMPT_LABEL)} error={flag(ERROR_LABEL)}"
    )
