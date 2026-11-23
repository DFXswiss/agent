"""A TUI dummy for the delivery tests: Claude Code's input box, line reading and bracketed paste, nothing else.

Usage: ``python3 tui_dummy.py <output file> [<restore delay in seconds>]`` inside a tmux pane. The terminal is put
into raw mode with bracketed paste on. The screen shows a rule, the input ``<marker> <text>`` and a rule, like Claude
Code 2.1.330 (observed against the real TUI, KOR-09):

* The input is wrapped into visual rows of the pane width minus the marker (hard, also inside words); each line of
  the input starts a new row. Printable characters are appended.
* ``C-u`` removes the last visual row (with the line break before it); ``C-e`` does nothing (the cursor is always at
  the end). A collapsed paste is one unit: ``C-u`` removes it whole.
* A bracketed paste is inserted as one piece (carriage returns of ``paste-buffer`` become newlines). A paste with
  more than two line breaks or more than 800 characters is shown collapsed as ``[Pasted text #<n> +<lines> lines]``
  (``[Pasted text #<n>]`` without line breaks); shorter ones are shown as they are.
* ``Enter`` submits; the submitted input is the current turn. ``Escape`` while a turn runs interrupts it and puts
  the turn's input back in front of the input, after the restore delay (default 0) - Claude Code puts an interrupted
  prompt back into its input box.
* A submission the UserPromptSubmit hook blocked is put back in front of the input as well: the test's stand-in
  for the hook appends ``{"text": ..., "accepted": false}`` lines to ``<output file>.verdicts``.

Every submission and every ``Escape`` is appended to the output file as one JSON line: ``{"submit": text}`` or
``{"escape": true}``.
"""

from __future__ import annotations

import codecs
import json
import os
import select
import shutil
import sys
import termios
import time
import tty
from dataclasses import dataclass

MARK = "\u276f"
RULE = "\u2500" * 60
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
COLLAPSE_LINES = 2
COLLAPSE_CHARS = 800


@dataclass(frozen=True)
class Unit:
    """One character of the input, or one collapsed paste (``text`` is submitted, ``label`` is shown)."""

    text: str
    label: str


class Dummy:
    def __init__(self, output: str, restore_delay: float = 0.0) -> None:
        self.output = output
        self.verdicts = output + ".verdicts"
        self.restore_delay = restore_delay
        self.units: list[Unit] = []
        self.turn: list[Unit] | None = None
        self.submissions: list[list[Unit]] = []
        self.restore_at: float | None = None
        self.verdicts_seen = 0
        self.pastes = 0
        self.pending = ""
        self.pasting: str | None = None
        self.width = max(20, shutil.get_terminal_size((160, 48)).columns - 2)

    def record(self, value: dict[str, object]) -> None:
        with open(self.output, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")

    # -- the input box ------------------------------------------------------------------------------------------

    def shown(self) -> str:
        return "".join(unit.label for unit in self.units)

    def rows(self) -> list[tuple[int, str]]:
        """Visual rows of the input as (start offset in ``shown()``, text)."""
        rows: list[tuple[int, str]] = []
        offset = 0
        for line in self.shown().split("\n"):
            if not line:
                rows.append((offset, ""))
            for start in range(0, len(line), self.width):
                rows.append((offset + start, line[start : start + self.width]))
            offset += len(line) + 1
        return rows

    def draw(self) -> None:
        rows = [text for _, text in self.rows()] or [""]
        body = "\r\n".join([f"{MARK} {rows[0]}", *(f"  {row}" for row in rows[1:])])
        sys.stdout.write(f"\x1b[2J\x1b[HTUI dummy\r\n{RULE}\r\n{body}\r\n{RULE}\r\n")
        sys.stdout.flush()

    def kill_row(self) -> None:
        """``C-u``: remove the last visual row and the line break before it; a collapsed paste goes whole."""
        if not self.units:
            return
        shown = self.shown()
        start = self.rows()[-1][0]
        cut = start - 1 if start > 0 and shown[start - 1] == "\n" else start
        while self.units and len(self.shown()) > cut:
            self.units.pop()

    def put_back(self, units: list[Unit]) -> None:
        self.units = [*units, *self.units]

    # -- input --------------------------------------------------------------------------------------------------

    def feed(self, data: str, idle: bool) -> None:
        self.pending += data
        while self.pending:
            if self.pasting is not None:
                end = self.pending.find(PASTE_END)
                if end < 0:
                    self.pasting += self.pending
                    self.pending = ""
                    return
                self.paste(self.pasting + self.pending[:end])
                self.pending = self.pending[end + len(PASTE_END) :]
                self.pasting = None
                continue
            char = self.pending[0]
            if char == "\x1b":
                if self.pending.startswith(PASTE_START):
                    self.pending = self.pending[len(PASTE_START) :]
                    self.pasting = ""
                    continue
                if PASTE_START.startswith(self.pending) and not idle:
                    return  # an incomplete sequence: wait for the rest
                self.pending = self.pending[1:]
                self.record({"escape": True})
                if self.turn is not None:
                    self.restore_at = time.monotonic() + self.restore_delay
                continue
            self.pending = self.pending[1:]
            if char == "\r":
                self.submit()
            elif char == "\x15":
                self.kill_row()
            elif char >= " " or char == "\t":
                self.units.append(Unit(char, char))

    def submit(self) -> None:
        text = "".join(unit.text for unit in self.units)
        self.record({"submit": text})
        self.submissions.append(self.units)
        self.turn, self.units, self.restore_at = self.units, [], None

    def paste(self, text: str) -> None:
        text = text.replace("\r", "\n")
        breaks = text.count("\n")
        if breaks > COLLAPSE_LINES or len(text) > COLLAPSE_CHARS:
            self.pastes += 1
            label = f"[Pasted text #{self.pastes} +{breaks} lines]" if breaks else f"[Pasted text #{self.pastes}]"
            self.units.append(Unit(text, label))
        else:
            self.units.extend(Unit(char, char) for char in text)

    # -- background events --------------------------------------------------------------------------------------

    def tick(self) -> bool:
        """Due restores; ``True`` when the input changed."""
        changed = False
        if self.restore_at is not None and time.monotonic() >= self.restore_at and self.turn is not None:
            self.put_back(self.turn)
            self.turn, self.restore_at = None, None
            changed = True
        if os.path.exists(self.verdicts):
            with open(self.verdicts, encoding="utf-8") as handle:
                verdicts = [json.loads(line) for line in handle.read().splitlines()]
            for verdict in verdicts[self.verdicts_seen :]:
                if not verdict["accepted"]:
                    blocked = next(
                        (
                            units
                            for units in reversed(self.submissions)
                            if "".join(unit.text for unit in units) == verdict["text"]
                        ),
                        [Unit(char, char) for char in verdict["text"]],
                    )
                    self.put_back(blocked)
                    self.turn = None
                    changed = True
            self.verdicts_seen = len(verdicts)
        return changed


def main() -> int:
    restore_delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    dummy = Dummy(sys.argv[1], restore_delay)
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    tty.setraw(descriptor)
    decoder = codecs.getincrementaldecoder("utf-8")()
    sys.stdout.write("\x1b[?2004h")
    try:
        dummy.draw()
        while True:
            ready, _, _ = select.select([descriptor], [], [], 0.1)
            if not ready:
                changed = dummy.tick()
                if dummy.pending:
                    dummy.feed("", idle=True)
                    changed = True
                if changed:
                    dummy.draw()
                continue
            data = os.read(descriptor, 65536)
            if not data:
                return 0
            dummy.feed(decoder.decode(data), idle=False)
            dummy.tick()
            dummy.draw()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    sys.exit(main())
