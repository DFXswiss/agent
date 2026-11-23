"""The terminal screen of the login command (05-runner.md section 11.2, KOR-21).

`claude setup-token` draws a terminal UI: it places words with cursor positioning instead of writing spaces, erases
lines, moves the cursor and overwrites single changed cells (spinner, status line). Stripping the control sequences
from the raw output therefore glues words together and leaves stale frames behind. `Screen` replays the output on a
small terminal of the PTY's window size, so the login reads what a person would see: rows of cells with real spaces.

Implemented (exactly what the login program uses; everything else has no effect):

- printable characters, wide characters (two cells, `unicodedata.east_asian_width` W/F), combining characters (joined
  to the previous cell), auto-wrap at the right margin with a deferred wrap (xterm: the cursor stays on the last
  column until the next character);
- C0: CR, LF (scrolls at the bottom row), BS, HT (tab stops every 8 columns); every other control is ignored;
- ESC 7 / ESC 8 (save and restore the cursor); CSI `A B C D E F G H f d J K` with their parameters and defaults;
- CSI `J` mode 3 (scrollback only), CSI with a private marker (`?`, `>`, `<`, `=`) or an intermediate, SGR and every
  other CSI, OSC, DCS, SOS, PM, APC (terminated by BEL or ST) and other ESC sequences are consumed without effect.

The parser keeps its state between `feed` calls, so output split anywhere (also inside a sequence or a UTF-8
character, `PtyProcess` decodes incrementally) gives the same screen. The screen holds no history: lines that scroll
off the top are gone, as on a terminal without scrollback.
"""

from __future__ import annotations

import unicodedata

TAB = 8
MAX_PARAM = 9999
"""Numeric CSI parameters are clamped (a cursor never moves beyond the screen anyway)."""
MAX_PARAMS_TEXT = 64
"""Characters of a CSI parameter string kept; a longer one is consumed but its rest is ignored."""

_GROUND, _ESC, _ESC_INTER, _CSI, _STRING, _STRING_ESC = range(6)
_STRING_INTRODUCERS = "]PX^_"
"""OSC, DCS, SOS, PM, APC: a string up to BEL or ST."""


def char_width(char: str) -> int:
    """Cells of `char`: 0 for combining and format characters, 2 for wide and full-width ones, else 1."""
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


class Screen:
    """A `rows` x `columns` terminal fed with decoded output (see the module docstring)."""

    def __init__(self, rows: int, columns: int) -> None:
        if rows < 1 or columns < 2:
            raise ValueError("a screen needs at least one row and two columns")
        self.rows = rows
        self.columns = columns
        self.cells = [self._blank_row() for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.wrap_pending = False
        self.saved = (0, 0)
        self._state = _GROUND
        self._params = ""

    def _blank_row(self) -> list[str]:
        return [" "] * self.columns

    # --- reading -----------------------------------------------------------------------------------------------------

    def lines(self) -> list[str]:
        """The rows as text: cells joined (the second cell of a wide character is empty), trailing blanks removed."""
        return ["".join(row).rstrip() for row in self.cells]

    def text(self) -> str:
        return "\n".join(self.lines())

    # --- feeding -----------------------------------------------------------------------------------------------------

    def feed(self, text: str) -> None:
        for char in text:
            state = self._state
            if state == _GROUND:
                self._ground(char)
            elif state == _ESC:
                self._escape(char)
            elif state == _ESC_INTER:
                if not "\x20" <= char <= "\x2f":
                    self._state = _GROUND
            elif state == _CSI:
                self._csi_char(char)
            elif state == _STRING:
                if char == "\x07":
                    self._state = _GROUND
                elif char == "\x1b":
                    self._state = _STRING_ESC
            elif char == "\\":  # _STRING_ESC: ST ends the string
                self._state = _GROUND
            else:  # any other ESC sequence ends the string and starts anew
                self._escape(char)

    def _ground(self, char: str) -> None:
        if char == "\x1b":
            self._state = _ESC
        elif char == "\r":
            self._move(self.row, 0)
        elif char == "\n":
            self._line_feed()
        elif char == "\b":
            self._move(self.row, self.col - 1)
        elif char == "\t":
            self._move(self.row, (self.col // TAB + 1) * TAB)
        elif char < " " or "\x7f" <= char <= "\x9f":
            return
        else:
            self._print(char)

    def _escape(self, char: str) -> None:
        self._state = _GROUND
        if char == "[":
            self._state = _CSI
            self._params = ""
        elif char in _STRING_INTRODUCERS:
            self._state = _STRING
        elif "\x20" <= char <= "\x2f":
            self._state = _ESC_INTER
        elif char == "7":
            self.saved = (self.row, self.col)
        elif char == "8":
            self._move(*self.saved)
        elif char == "\x1b":
            self._state = _ESC

    def _csi_char(self, char: str) -> None:
        if "\x40" <= char <= "\x7e":
            self._state = _GROUND
            self._csi(char, self._params)
        elif "\x20" <= char <= "\x3f":
            if len(self._params) < MAX_PARAMS_TEXT:
                self._params += char
        elif char == "\x1b":
            self._state = _ESC
        elif char < " ":
            self._ground(char)  # a C0 control inside a CSI is executed (VT500 parser)

    # --- operations --------------------------------------------------------------------------------------------------

    def _move(self, row: int, col: int) -> None:
        self.row = min(max(row, 0), self.rows - 1)
        self.col = min(max(col, 0), self.columns - 1)
        self.wrap_pending = False

    def _line_feed(self) -> None:
        self.wrap_pending = False
        if self.row == self.rows - 1:
            del self.cells[0]
            self.cells.append(self._blank_row())
        else:
            self.row += 1

    def _clear(self, row: int, start: int, end: int) -> None:
        """Blank the cells `start` .. `end - 1` of `row`, and the other half of a wide character cut at either end."""
        cells = self.cells[row]
        if start > 0 and cells[start] == "":
            cells[start - 1] = " "
        if end < self.columns and cells[end] == "":
            cells[end] = " "  # the second half of a wide character whose first half goes
        for i in range(start, end):
            cells[i] = " "

    def _print(self, char: str) -> None:
        width = char_width(char)
        if width == 0:
            self._combine(char)
            return
        if self.wrap_pending:
            self._move(self.row, 0)
            self._line_feed()
        if width == 2 and self.col == self.columns - 1:
            self._clear(self.row, self.col, self.col + 1)  # no room for both halves: xterm wraps first
            self._move(self.row, 0)
            self._line_feed()
        self._clear(self.row, self.col, self.col + width)
        cells = self.cells[self.row]
        cells[self.col] = char
        if width == 2:
            cells[self.col + 1] = ""
        last = self.col + width - 1
        if last == self.columns - 1:
            self.col = last
            self.wrap_pending = True
        else:
            self.col = last + 1

    def _combine(self, char: str) -> None:
        """Join a zero-width character to the character left of the cursor (nothing there: dropped)."""
        cells = self.cells[self.row]
        col = self.col if self.wrap_pending else self.col - 1
        if col >= 0 and cells[col] == "":
            col -= 1
        if col >= 0 and cells[col] != " ":
            cells[col] += char

    def _csi(self, final: str, params: str) -> None:
        if params[:1] in ("?", ">", "<", "=") or any(c in params for c in "\x20!\"#$%&'()*+,-./"):
            return  # private modes, device queries, intermediates: no effect on the screen
        values = self._numbers(params)

        def arg(index: int, default: int) -> int:
            value = values[index] if index < len(values) else None
            return default if value is None or (value == 0 and default == 1) else value

        n = arg(0, 1)
        if final == "A":
            self._move(self.row - n, self.col)
        elif final == "B":
            self._move(self.row + n, self.col)
        elif final == "C":
            self._move(self.row, self.col + n)
        elif final == "D":
            self._move(self.row, self.col - n)
        elif final == "E":
            self._move(self.row + n, 0)
        elif final == "F":
            self._move(self.row - n, 0)
        elif final == "G":
            self._move(self.row, n - 1)
        elif final in ("H", "f"):
            self._move(n - 1, arg(1, 1) - 1)
        elif final == "d":
            self._move(n - 1, self.col)
        elif final == "J":
            self._erase_display(arg(0, 0))
        elif final == "K":
            self._erase_line(arg(0, 0))

    def _numbers(self, params: str) -> list[int | None]:
        values: list[int | None] = []
        for part in params.split(";"):
            digits = part.split(":", 1)[0]
            values.append(min(int(digits), MAX_PARAM) if digits.isdigit() else None)
        return values

    def _erase_line(self, mode: int) -> None:
        if mode == 0:
            self._clear(self.row, self.col, self.columns)
        elif mode == 1:
            self._clear(self.row, 0, self.col + 1)
        elif mode == 2:
            self._clear(self.row, 0, self.columns)
        self.wrap_pending = False

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_line(0)
            for row in range(self.row + 1, self.rows):
                self.cells[row] = self._blank_row()
        elif mode == 1:
            self._erase_line(1)
            for row in range(self.row):
                self.cells[row] = self._blank_row()
        elif mode == 2:
            self.cells = [self._blank_row() for _ in range(self.rows)]
        # mode 3 erases the scrollback only, which this screen does not keep
        self.wrap_pending = False
