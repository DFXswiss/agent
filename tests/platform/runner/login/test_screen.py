"""U-RUN-login (screen, KOR-21): the terminal screen the login reads, against a real recording of `claude setup-token`
2.1.330 and sequence by sequence.

`fixtures/setup-token-2.1.330-wrong-code.json` is the raw PTY output of the real program (window 50 x 1000, no
account, a deliberately wrong code; the short authorize URL is redacted): `stream` from the start until 12 s after the
code was typed, `offset_code_typed` the length of the output when the code was typed (the program had already
answered by the time the PTY was read, so the last frame with the input field ends at the first erase before it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_runner.login import setup_token
from agent_runner.login.screen import MAX_PARAMS_TEXT, Screen, char_width

RECORDING = json.loads((Path(__file__).parent / "fixtures" / "setup-token-2.1.330-wrong-code.json").read_text())
STREAM: str = RECORDING["stream"]
FIRST_ERASE = STREAM.index("\x1b[3C\x1b[2A")
ERROR_ROW = (
    " ✘ OAuth error: That code wasn't accepted. It may be incomplete, expired, already used, or from an earlier "
    "sign-in attempt. Sign in again to get a new code."
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
"""The control-sequence filter the relay used before KOR-21 (kept here to show what it made of the screen)."""


def screen_of(text: str, rows: int = 50, columns: int = 1000) -> Screen:
    screen = Screen(rows, columns)
    screen.feed(text)
    return screen


def test_the_recording_is_of_the_golden_pin_in_the_runners_window() -> None:
    assert RECORDING["claude_code"] == setup_token.CHECKED_VERSION
    assert RECORDING["winsize"] == {"rows": 50, "cols": 1000}
    assert RECORDING["running_after_error"] is True


def test_stripping_the_control_sequences_glues_the_words_of_the_real_program() -> None:
    stripped = ANSI.sub("", STREAM)
    assert "Pastecodehereifprompted" in stripped
    assert setup_token.PROMPT_LABEL not in stripped
    assert "OAuth error: That code" in stripped  # written in one piece by chance, the rest is glued
    assert "wasn'taccepted.Itmay" in stripped


def test_the_input_field_of_the_real_program_is_on_the_reconstructed_screen() -> None:
    lines = screen_of(STREAM[:FIRST_ERASE]).lines()
    assert " Paste code here if prompted" in lines
    box = lines.index(" Paste code here if prompted") + 1
    assert (lines[box][:3], lines[box][-2:], len(lines[box])) == (" ╭─", "─╮", 1000)
    assert (lines[box + 1][:2], lines[box + 1][-1]) == (" │", "│")
    assert " Browser didn't open? Use the URL below to sign in (c to copy)" in lines
    assert "https://claude.ai/oauth/authorize?REDACTEDSHORTCODE" in lines
    assert lines[0] == "Welcome to Claude Code v2.1.330"
    assert setup_token.shown(lines, setup_token.PROMPT_LABEL)
    assert not setup_token.shown(lines, setup_token.ERROR_LABEL)


def test_the_error_of_the_real_program_is_on_the_screen_word_for_word() -> None:
    screen = screen_of(STREAM)
    lines = screen.lines()
    assert ERROR_ROW in lines
    assert lines[lines.index(ERROR_ROW) + 2] == " Press Enter to retry"
    assert not setup_token.shown(lines, setup_token.PROMPT_LABEL)  # the input field was erased
    assert not any("Finishing" in line for line in lines)  # the status row was overwritten
    assert setup_token.shape(lines) == "rows=18 marker=no token_blocks=0 prompt=no error=yes"
    assert setup_token.parse_token(lines) is None


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64])
def test_output_split_anywhere_gives_the_same_screen(size: int) -> None:
    screen = Screen(50, 1000)
    for i in range(0, len(STREAM), size):
        screen.feed(STREAM[i : i + size])
    assert screen.lines() == screen_of(STREAM).lines()


# --- printable characters ------------------------------------------------------------------------------------------


def test_char_width() -> None:
    chars = ("a", "✻", "…", "─", "語", "\uff26", "\u0301", "\u200d")  # \uff26: full-width F
    assert [char_width(c) for c in chars] == [1, 1, 1, 1, 2, 2, 0, 0]


def test_a_too_small_screen_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one row"):
        Screen(0, 10)
    with pytest.raises(ValueError, match="two columns"):
        Screen(3, 1)


def test_text_wraps_at_the_right_margin_only_with_the_next_character() -> None:
    screen = screen_of("abcde", rows=3, columns=5)
    assert (screen.row, screen.col, screen.wrap_pending) == (0, 4, True)
    screen.feed("\r\nx")
    assert screen.lines() == ["abcde", "x", ""]
    screen = screen_of("abcdefg", rows=3, columns=5)
    assert screen.lines() == ["abcde", "fg", ""]
    screen = screen_of("abcde\x1b[2Dz", rows=3, columns=5)
    assert screen.lines() == ["abzde", "", ""]  # a cursor movement cancels the pending wrap


def test_line_feed_scrolls_at_the_bottom_and_the_top_row_is_gone() -> None:
    screen = screen_of("1\r\n2\r\n3\r\n4", rows=3, columns=4)
    assert screen.lines() == ["2", "3", "4"]
    screen = screen_of("abcdefgh", rows=1, columns=4)
    assert screen.lines() == ["efgh"]
    assert screen.text() == "efgh"


def test_line_feed_keeps_the_column() -> None:
    assert screen_of("ab\ncd", rows=2, columns=6).lines() == ["ab", "  cd"]


def test_wide_characters_take_two_cells() -> None:
    screen = screen_of("語x", rows=2, columns=6)
    assert screen.cells[0][:3] == ["語", "", "x"]
    assert screen.lines()[0] == "語x"
    assert screen_of("abc語", rows=2, columns=4).lines() == ["abc", "語"]  # no room for both halves: wrap first
    assert screen_of("ab語", rows=2, columns=4).lines() == ["ab語", ""]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("語語\x1b[2Gx", " x語"),  # overwriting the second half blanks the first
        ("語語\x1b[1Gx", "x 語"),  # overwriting the first half blanks the second
        ("a語\x1b[2G語", "a語"),
        ("語語\x1b[2G\x1b[1K", "  語"),
        ("語語\x1b[3G\x1b[K", "語"),
        ("ab語\x1b[3G語", "ab語"),
    ],
)
def test_overwriting_half_of_a_wide_character_blanks_the_other_half(text: str, expected: str) -> None:
    assert screen_of(text, rows=1, columns=8).lines() == [expected]


def test_combining_characters_join_the_previous_cell() -> None:
    screen = screen_of("éx", rows=1, columns=6)
    assert screen.lines() == ["éx"]
    assert screen_of("́a", rows=1, columns=6).lines() == ["a"]  # nothing to join: dropped
    assert screen_of("abcd́", rows=1, columns=4).lines() == ["abcd́"]  # with a pending wrap
    assert screen_of("語́", rows=1, columns=6).lines() == ["語́"]
    assert screen_of(" ́", rows=1, columns=6).lines() == [""]


# --- C0 controls ---------------------------------------------------------------------------------------------------


def test_carriage_return_backspace_and_tab() -> None:
    assert screen_of("abc\rX", rows=1, columns=10).lines() == ["Xbc"]
    assert screen_of("abc\b\bX\b\b\b\bY", rows=1, columns=10).lines() == ["YXc"]
    assert screen_of("a\tb\tc", rows=1, columns=20).lines() == ["a       b       c"]
    assert screen_of("a\t\t\tb", rows=1, columns=12).lines() == ["a          b"]  # the last tab stop is the margin
    assert screen_of("ab\x07\x00\x01\x7f\x85c", rows=1, columns=10).lines() == ["abc"]  # other controls: no effect


# --- CSI -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moves", "where"),
    [
        ("\x1b[A", (1, 4)),
        ("\x1b[2A", (0, 4)),
        ("\x1b[9A", (0, 4)),
        ("\x1b[0A", (1, 4)),
        ("\x1b[B", (3, 4)),
        ("\x1b[9B", (4, 4)),
        ("\x1b[C", (2, 5)),
        ("\x1b[3C", (2, 7)),
        ("\x1b[99C", (2, 9)),
        ("\x1b[D", (2, 3)),
        ("\x1b[99D", (2, 0)),
        ("\x1b[E", (3, 0)),
        ("\x1b[2E", (4, 0)),
        ("\x1b[F", (1, 0)),
        ("\x1b[G", (2, 0)),
        ("\x1b[7G", (2, 6)),
        ("\x1b[H", (0, 0)),
        ("\x1b[3;5H", (2, 4)),
        ("\x1b[;5H", (0, 4)),
        ("\x1b[4H", (3, 0)),
        ("\x1b[2;3f", (1, 2)),
        ("\x1b[99;99H", (4, 9)),
        ("\x1b[d", (0, 4)),
        ("\x1b[4d", (3, 4)),
        ("\x1b[12345678G", (2, 9)),
        ("\x1b[2:1G", (2, 1)),
    ],
)
def test_cursor_movements_with_parameters_and_defaults(moves: str, where: tuple[int, int]) -> None:
    screen = screen_of("\x1b[3;5H" + moves, rows=5, columns=10)
    assert (screen.row, screen.col) == where


FULL = "\x1b[1;1Haaaaa\x1b[2;1Hbbbbb\x1b[3;1Hccccc\x1b[2;3H"


@pytest.mark.parametrize(
    ("erase", "expected"),
    [
        ("\x1b[K", ["aaaaa", "bb", "ccccc"]),
        ("\x1b[0K", ["aaaaa", "bb", "ccccc"]),
        ("\x1b[1K", ["aaaaa", "   bb", "ccccc"]),
        ("\x1b[2K", ["aaaaa", "", "ccccc"]),
        ("\x1b[5K", ["aaaaa", "bbbbb", "ccccc"]),
        ("\x1b[J", ["aaaaa", "bb", ""]),
        ("\x1b[1J", ["", "   bb", "ccccc"]),
        ("\x1b[2J", ["", "", ""]),
        ("\x1b[3J", ["aaaaa", "bbbbb", "ccccc"]),
        ("\x1b[7J", ["aaaaa", "bbbbb", "ccccc"]),
    ],
)
def test_erasing_in_line_and_display(erase: str, expected: list[str]) -> None:
    screen = screen_of(FULL + erase, rows=3, columns=5)
    assert screen.lines() == expected
    assert (screen.row, screen.col) == (1, 2)  # erasing never moves the cursor


def test_erasing_cancels_a_pending_wrap() -> None:
    screen = screen_of("abcde\x1b[K", rows=2, columns=5)
    assert not screen.wrap_pending
    screen.feed("x")
    assert screen.lines() == ["abcdx", ""]


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b[?25l",
        "\x1b[?2004h",
        "\x1b[>0q",
        "\x1b[?u",
        "\x1b[=1c",
        "\x1b[<1u",
        "\x1b[c",
        "\x1b[7m",
        "\x1b[38;2;1;2;3m",
        "\x1b[2 q",
        "\x1b[r",
        "\x1b[5S",
        "\x1b[1@",
    ],
)
def test_other_csi_sequences_have_no_effect(sequence: str) -> None:
    screen = screen_of("ab" + sequence + "c", rows=2, columns=6)
    assert screen.lines() == ["abc", ""]


def test_a_long_parameter_string_is_cut_and_a_control_inside_a_csi_is_executed() -> None:
    assert screen_of("ab\x1b[" + "1" * (MAX_PARAMS_TEXT + 10) + "Gc", rows=1, columns=6).lines() == ["ab   c"]
    assert screen_of("abc\x1b[\r2Cx", rows=1, columns=6).lines() == ["abx"]
    assert screen_of("abc\x1b[1\x1b[2Gx", rows=1, columns=6).lines() == ["axc"]  # ESC abandons the CSI
    assert screen_of("abc\x1b[2\x7f\u00e4Gx", rows=1, columns=6).lines() == ["axc"]  # DEL, non-ASCII: ignored


# --- other escape sequences and strings ----------------------------------------------------------------------------


def test_save_and_restore_the_cursor() -> None:
    assert screen_of("ab\x1b7\r\ncd\x1b8x", rows=2, columns=6).lines() == ["abx", "cd"]
    assert screen_of("\x1b7\x1b[r\x1b8x", rows=2, columns=6).lines() == ["x", ""]


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b]0;title\x07",
        "\x1b]8;id=1;https://example.test\x1b\\",
        "\x1b]11;?\x07",
        "\x1bP1$r\x1b\\",
        "\x1b_payload\x1b\\",
        "\x1b^private\x07",
        "\x1bXstring\x07",
        "\x1b(B",
        "\x1b$(B",
        "\x1b#8",
        "\x1b=",
        "\x1bc",
        "\x1b\x1b[1m",
    ],
)
def test_strings_and_other_escape_sequences_have_no_effect(sequence: str) -> None:
    assert screen_of("ab" + sequence + "c", rows=1, columns=6).lines() == ["abc"]


def test_an_escape_sequence_inside_a_string_ends_it() -> None:
    assert screen_of("ab\x1b]0;title\x1b[1Gc", rows=1, columns=6).lines() == ["cb"]
    assert screen_of("ab\x1b]8;;https://x\x07link\x1b]8;;\x07", rows=1, columns=10).lines() == ["ablink"]
