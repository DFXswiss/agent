"""U-RUN-login (token screen, KOR-21): the token is read from the reconstructed screen, in every way the program may
draw it, and a part of a token is never taken."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runner.login import setup_token
from agent_runner.login.screen import Screen

TOKEN = setup_token.TOKEN_PREFIX + "01-" + "a1B2_c3-D4" * 9 + "AA"
VERSIONS = Path(__file__).resolve().parents[4] / "images" / "versions.json"


def lines_of(text: str, rows: int = 50, columns: int = 1000) -> list[str]:
    screen = Screen(rows, columns)
    screen.feed(text)
    return screen.lines()


def placed(row: str) -> str:
    """`row` drawn like Claude Code 2.1.330: each word placed with CHA instead of the spaces before it."""
    out, col = [], 0
    for i, char in enumerate(row):
        if char != " ":
            if col != i:
                out.append(f"\x1b[{i + 1}G")
            out.append(char)
            col = i + 1
    return "".join(out) + "\r\r\n"


SUCCESS = [
    " ✓ Long-lived authentication token created",
    "",
    " Your OAuth token (valid for 1 year):",
    "",
    f" {TOKEN}",
    "",
    " Store this token securely. You won't be able to see it again.",
    "",
    " Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>",
]


def test_the_labels_were_checked_against_the_pin_of_the_golden() -> None:
    """When the pin moves, check the login against the new version (claude-code-upgrade-gate.md), then follow here."""
    assert json.loads(VERSIONS.read_text())["claude_code"] == setup_token.CHECKED_VERSION


def test_whole_rows_with_spaces() -> None:
    assert setup_token.parse_token(lines_of("\x1b[32m" + "\r\n".join(SUCCESS) + "\x1b[0m\r\n")) == TOKEN


def test_rows_placed_with_cursor_positioning_like_2_1_330() -> None:
    raw = "".join(placed(row) for row in SUCCESS)
    assert "Your OAuth token" not in raw
    lines = lines_of(raw)
    assert " Your OAuth token (valid for 1 year):" in lines
    assert setup_token.parse_token(lines) == TOKEN


def test_a_frame_drawn_over_an_earlier_frame_with_partly_equal_cells() -> None:
    """The status frame (spinner, "Finishing sign-in…", the URL and the input field) is overwritten cell by cell: only
    changed cells are written, spaces only where a character stood, the rest of a row with ESC[K."""
    before = [
        " · Finishing sign-in…",
        "",
        " Your code is being checked (valid for a moment)",
        "",
        " https://claude.ai/oauth/authorize?" + "x" * 140,
        "",
        " Paste code here if prompted",
    ]
    raw = "".join(placed(row) for row in before) + f"\r\x1b[{len(before)}A"
    for old, new in zip(before + [""] * 2, SUCCESS, strict=True):
        cells = []
        for i, char in enumerate(new):
            if i >= len(old) or old[i] != char:
                cells.append(f"\x1b[{i + 1}G{char}")
        tail = "\x1b[K" if len(old) > len(new) else ""
        raw += "".join(cells) + (f"\x1b[{len(new) + 1}G" if tail else "") + tail + "\r\n"
    lines = lines_of(raw)
    assert lines[: len(SUCCESS)] == [row.rstrip() for row in SUCCESS]
    assert setup_token.parse_token(lines) == TOKEN


def test_a_token_over_two_rows_in_a_narrow_window() -> None:
    narrow = [row for row in SUCCESS if TOKEN not in row]
    raw = "\r\n".join(narrow[:4]) + "\r\n " + TOKEN + "\r\n" + "\r\n".join(narrow[4:])
    lines = lines_of(raw, rows=30, columns=80)  # the terminal wraps the token at the margin
    assert [line for line in lines if line.startswith(" sk-")] == [" " + TOKEN[:79]]
    assert setup_token.parse_token(lines) == TOKEN
    wrapped = ["", " Your OAuth token (valid for 1 year):", "", " " + TOKEN[:50], " " + TOKEN[50:], "", " Store"]
    assert setup_token.parse_token(wrapped) == TOKEN  # hard-wrapped by the program itself


@pytest.mark.parametrize(
    "rows",
    [
        [" Your OAuth token (valid for 1 year):"],
        [" Your OAuth token (valid for 1 year):", "", ""],
        [" Your OAuth token (valid for 1 year):", "", " not-a-token"],
        [" Your OAuth token (valid for 1 year):", "", " " + TOKEN[:20]],
        [" Your OAuth token (valid for 1 year):", "", " " + TOKEN, " Store this token securely."],
        [" Your OAuth token (valid for 1 year):", "", " sk-other-token-value-0000000000000", "", " " + TOKEN],
        [" Your OAuth token (valid for 1 year):", "", " " + TOKEN + " (copy)"],
        # a token split by an empty row: the first piece alone looks like a whole (shorter) token
        [" Your OAuth token (valid for 1 year):", "", " " + TOKEN[:60], "", " " + TOKEN[60:], "", " Store this"],
    ],
)
def test_after_the_marker_only_a_whole_token_counts(rows: list[str]) -> None:
    assert setup_token.parse_token(rows) is None


def test_the_token_right_below_the_marker() -> None:
    assert setup_token.parse_token(["Your OAuth token:", TOKEN, ""]) == TOKEN


def test_without_the_marker_exactly_one_block_that_is_a_whole_token() -> None:
    reworded = [" Here is your long-lived key:", "", " " + TOKEN[:60], " " + TOKEN[60:], "", " Keep it safe."]
    assert setup_token.parse_token(reworded) == TOKEN
    assert setup_token.parse_token(["Login", "", " " + TOKEN, "", "Done"]) == TOKEN  # short lone words do not count


@pytest.mark.parametrize(
    "rows",
    [
        [],
        ["no block at all"],
        [" " + TOKEN, "", " " + TOKEN],  # two candidates
        [" Key:", " " + TOKEN],  # the token shares its block with text
        [" " + TOKEN[:30] + "…"],  # a row cut by the renderer
        [" " + TOKEN[:40], "", " " + TOKEN[40:]],  # a token split by an empty row
        [" " + TOKEN, "", " sk-" + "ant-oat01-x"],  # a second piece with the prefix
        [" export CLAUDE_CODE_OAUTH_TOKEN=" + TOKEN],
    ],
)
def test_without_the_marker_anything_else_yields_none(rows: list[str]) -> None:
    assert setup_token.parse_token(rows) is None


def test_the_shape_names_counts_and_flags_never_text() -> None:
    rows = [
        " https://claude.ai/oauth/authorize?code=true&state=secret-state",
        "",
        " Paste code here if prompted",
        " │ the-typed-code#secret-state │",
        "",
        " Your OAuth token (valid for 1 year):",
        "",
        " " + TOKEN,
        "",
        " ✘ OAuth error: something",
    ]
    shape = setup_token.shape(rows)
    assert shape == "rows=6 marker=yes token_blocks=1 prompt=yes error=yes"
    for part in ("secret", "typed", "http", TOKEN[:12], "something"):
        assert part not in shape
    assert setup_token.shape([]) == "rows=0 marker=no token_blocks=0 prompt=no error=no"


def test_blocks_are_runs_of_non_empty_rows() -> None:
    assert setup_token.blocks(["", " a", "b ", "  ", "c"]) == [(1, ["a", "b"]), (4, ["c"])]
