"""Terminal recorder of the control process (05-runner.md section 7 and 4.6 defects 5 and 6; ADR-016).

The pane of the tmux session ``s-<id>`` is piped into a FIFO with ``tmux pipe-pane -O`` (the tmux server runs
``cat >> <fifo>`` as root); the recorder thread of the control process reads the FIFO. The FIFO lives in the
root-only directory ``FIFO_DIR`` and is opened read-write, so it never reports end-of-file while ``cat`` is replaced.

``Recorder`` (pure) turns bytes into asciicast v2 output events:

* an incremental UTF-8 decoder (``errors="replace"``) keeps a character split across reads for the next read
* events ``[t_ms, "o", data]`` with ``t_ms`` in milliseconds since the session's recording started, never
  decreasing; a restart of the recorder (control process restarted, session resumed) continues at the last written
  offset and writes a marker ``[t_ms, "m", "recorder restarted"]`` instead of a new header
* an event's data is cut into pieces of at most ``MAX_PIECE`` UTF-8 bytes and as many bytes of JSON (the contract
  allows 16384 characters per event)
* ``take()`` groups the buffered events into ``session.terminal {offset_ms_start, frames, cols, rows}`` bodies of at
  most ``MAX_FRAMES`` events and ``TerminalLimits.record_json`` bytes of body JSON (never more than
  ``MAX_BODY_JSON``): the runner encrypts exactly that JSON, and JSON escapes a control character as six bytes
  (``\u001b``), so only the encoded size bounds the sealed event (E8: 64 KiB, 04-crypto.md section 6.4;
  decisions/KOR-02.md). ``take(whole=True)`` returns only the bodies that reached a bound and keeps the rest.

``TerminalRecorder`` (one per session, a thread; ADR-086 section 3, ``limits.TerminalLimits``): a record is written
once the output has been quiet for ``quiet_ms`` (100 ms); under sustained output the buffered events grow until the
first of them is ``max_age_ms`` old (1 s) - then everything is written - or until they fill a record of
``record_json`` bytes (60 KiB) - then the full records are written and the rest keeps growing. The same bytes in far
fewer events, in their order, nothing dropped. The bodies are journaled and the same events appended to the
session's asciicast v2 file ``<session base>/terminal.cast`` (root 0600; times in seconds = ``t_ms / 1000``), the
copy the history is compared with (E2E-12). The first output of the pane calls ``on_output`` (the "TUI up" signal).
"""

from __future__ import annotations

import codecs
import contextlib
import json
import os
import select
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from agent_guest import tmux
from agent_guest.journal import Journal
from agent_guest.limits import TERMINAL, TerminalLimits

MAX_PIECE = 16 * 1024
"""UTF-8 bytes of the data of one event (contract: at most 16384 characters)."""
AEAD_TAG_BYTES = 16
MAX_BODY_JSON = 64 * 1024 - AEAD_TAG_BYTES
"""Body JSON of one record: the sealed ``session.terminal`` (body JSON plus the 16-byte tag) stays within E8."""
MAX_PIECE_JSON = MAX_PIECE
"""JSON-escaped size of the data of one event."""
MAX_FRAMES = 4096
READ_CHUNK = 64 * 1024
MAX_DRAIN = 4 * READ_CHUNK
MAX_FINAL_DRAINS = 64
TAIL_BYTES = 256 * 1024
FIFO_DIR = "/run/agent-guest/capture"
CAST_NAME = "terminal.cast"
MARKER = "recorder restarted"
FIFO_FLAGS = os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC

Frame = list[Any]
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def json_size(value: object) -> int:
    """UTF-8 bytes of ``value`` as body JSON (sorted keys, no whitespace, non-ASCII kept; 04-crypto.md section 1)."""
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def split(text: str, limit: int = MAX_PIECE, json_limit: int = MAX_PIECE_JSON) -> list[str]:
    """``text`` in pieces of at most ``limit`` UTF-8 bytes and ``json_limit`` bytes as a JSON string (without the
    quotes), cut at character boundaries."""
    pieces = []
    while text:
        # a character has at least one byte, so the first ``limit`` characters hold the longest fitting prefix
        head = text[:limit].encode("utf-8")
        end = min(limit, len(head))
        while end < len(head) and head[end] & 0xC0 == 0x80:
            end -= 1
        # a single character is never cut, even when it alone exceeds a limit below four bytes
        count = max(1, len(head[:end].decode("utf-8")))
        if json_size(text[:count]) - 2 > json_limit:
            low, high = 1, count
            while low < high:
                middle = (low + high + 1) // 2
                if json_size(text[:middle]) - 2 <= json_limit:
                    low = middle
                else:
                    high = middle - 1
            count = low
        pieces.append(text[:count])
        text = text[count:]
    return pieces


class Recorder:
    def __init__(
        self, base_ms: int = 0, cols: int = tmux.COLS, rows: int = tmux.ROWS, record_json: int = TERMINAL.record_json
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.last_ms = base_ms
        self.record_json = min(record_json, MAX_BODY_JSON)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._frames: list[Frame] = []
        self._json = 0

    @property
    def pending_json(self) -> int:
        """Body JSON bytes of the buffered events (each event's JSON and its separator, without the body's keys)."""
        return self._json

    @property
    def full(self) -> bool:
        """The buffered events fill at least one record."""
        return self._json >= self.record_json or len(self._frames) > MAX_FRAMES

    @property
    def pending(self) -> bool:
        return bool(self._frames)

    def output(self, data: bytes, t_ms: int) -> None:
        self._add(t_ms, "o", self._decoder.decode(data))

    def marker(self, t_ms: int, text: str = MARKER) -> None:
        self._add(t_ms, "m", text)

    def _add(self, t_ms: int, code: str, text: str) -> None:
        if not text:
            return
        self.last_ms = max(self.last_ms, t_ms)
        for piece in split(text):
            frame: Frame = [self.last_ms, code, piece]
            self._frames.append(frame)
            self._json += json_size(frame) + 1

    def take(self, final: bool = False, whole: bool = False) -> list[dict[str, Any]]:
        """The buffered events as journal bodies; ``final`` also ends a character left incomplete; ``whole`` keeps
        the events of a last record that reached no bound buffered."""
        if final:
            self._add(self.last_ms, "o", self._decoder.decode(b"", final=True))
        bodies: list[dict[str, Any]] = []
        group: list[Frame] = []
        encoded = 0
        for frame in self._frames:
            frame_json = json_size(frame)
            if group and (len(group) == MAX_FRAMES or encoded + 1 + frame_json > self.record_json):
                bodies.append(self._body(group))
                group = []
            if not group:
                encoded = json_size(self._body([frame]))
            else:
                encoded += 1 + frame_json
            group.append(frame)
        if group and not whole:
            bodies.append(self._body(group))
            group = []
        self._frames = group
        self._json = sum(json_size(frame) + 1 for frame in group)
        return bodies

    def _body(self, frames: list[Frame]) -> dict[str, Any]:
        return {"offset_ms_start": frames[0][0], "frames": frames, "cols": self.cols, "rows": self.rows}


class CastFile:
    """The asciicast v2 copy of one session's terminal."""

    def __init__(self, path: str, cols: int = tmux.COLS, rows: int = tmux.ROWS) -> None:
        self.path = path
        self.cols = cols
        self.rows = rows

    def open(self, epoch: float) -> tuple[int, bool]:
        """``(last offset in ms, restarted)``: a new file gets the header, an existing one is continued."""
        try:
            with open(self.path, "rb") as handle:
                handle.seek(max(0, os.fstat(handle.fileno()).st_size - TAIL_BYTES))
                tail = handle.read()
        except FileNotFoundError:
            header = {"version": 2, "width": self.cols, "height": self.rows, "timestamp": int(epoch)}
            self._write([header])
            return 0, False
        return last_offset(tail), True

    def append(self, frames: list[Frame]) -> None:
        self._write([[frame[0] / 1000, frame[1], frame[2]] for frame in frames])

    def _write(self, values: list[Any]) -> None:
        text = "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)


def last_offset(tail: bytes) -> int:
    """Offset in ms of the last complete event line in ``tail`` (0 if there is none)."""
    for line in reversed(tail.split(b"\n")):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, list) and value and type(value[0]) in (int, float) and value[0] >= 0:
            seconds: float = value[0]
            return round(seconds * 1000)
    return 0


class TerminalRecorder:
    """Records the pane of one session (FIFO, journal, cast file)."""

    def __init__(
        self,
        journal: Journal,
        session_id: str,
        base: str,
        terminal: tmux.Tmux,
        run: Runner = subprocess.run,
        *,
        fifo_dir: str = FIFO_DIR,
        monotonic: Callable[[], float] = time.monotonic,
        on_output: Callable[[], None] | None = None,
        limits: TerminalLimits = TERMINAL,
    ) -> None:
        self.journal = journal
        self.limits = limits
        self.session_id = session_id
        self.terminal = terminal
        self.run = run
        self.fifo = f"{fifo_dir}/{session_id}.fifo"
        self.cast = CastFile(f"{base}/{CAST_NAME}")
        self.monotonic = monotonic
        self.on_output = on_output
        self.recorder = Recorder(record_json=limits.record_json)
        self._fd = -1
        self._started = 0.0
        self._base_ms = 0
        self._first: float | None = None
        self._last = 0.0
        self._now = False
        self._seen_output = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def now_ms(self) -> int:
        return self._base_ms + round((self.monotonic() - self._started) * 1000)

    def _tmux(self, *args: str) -> int:
        result = self.run(
            self.terminal.argv(*args),
            capture_output=True,
            text=True,
            timeout=tmux.TIMEOUT,
            check=False,
            env=tmux.CLIENT_ENV,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode

    def open(self) -> None:
        """Cast file, FIFO and ``pipe-pane``; raises ``OSError``/``TmuxError`` when the pane cannot be piped."""
        base_ms, restarted = self.cast.open(self.journal.clock.now())
        self._base_ms, self._started = base_ms, self.monotonic()
        self.recorder = Recorder(base_ms, record_json=self.limits.record_json)
        if restarted:
            self.recorder.marker(base_ms)
            self._now = True
        os.makedirs(os.path.dirname(self.fifo), mode=0o700, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo, 0o600)
        self._fd = os.open(self.fifo, FIFO_FLAGS)
        name = tmux.session_name(self.session_id)
        try:
            piped = self._tmux("pipe-pane", "-O", "-t", f"={name}:", f"exec cat >> {shlex.quote(self.fifo)}") == 0
        except (OSError, subprocess.SubprocessError):
            piped = False
        if not piped:
            self._close()
            raise tmux.TmuxError("pipe-pane failed")

    def drain(self) -> int:
        """Read what the FIFO holds now; returns the number of bytes."""
        total = 0
        while True:
            try:
                data = os.read(self._fd, READ_CHUNK)
            except BlockingIOError:
                break
            total += len(data)
            with self._lock:
                self.recorder.output(data, self.now_ms())
                self._last = self.monotonic()
                if self._first is None:
                    self._first = self._last
            if len(data) < READ_CHUNK or total >= MAX_DRAIN:
                # The FIFO is open for writing as well, so it never reads as ended; a short read means empty for now.
                # A bounded amount per call keeps flushes (full records) and the stop check going under constant
                # output.
                break
        if total and not self._seen_output:
            self._seen_output = True
            if self.on_output is not None:
                self.on_output()
        return total

    def _deadline(self) -> float | None:
        """When the buffered events are due by time (quiet or old enough); None without buffered output."""
        if self._first is None:
            return None
        return min(self._last + self.limits.quiet_ms / 1000, self._first + self.limits.max_age_ms / 1000)

    def _timed(self) -> bool:
        with self._lock:
            deadline = self._deadline()
            return self._now or (deadline is not None and self.monotonic() >= deadline)

    def _full(self) -> bool:
        with self._lock:
            return self.recorder.full

    def due(self) -> bool:
        """Something is to be written now (a full record, quiet or old output, the restart marker)."""
        with self._lock:
            if not self.recorder.pending:
                return False
        return self._full() or self._timed()

    def flush(self, final: bool = False, whole: bool = False) -> int:
        """Journal the buffered events (``whole``: only full records) and append them to the cast file; returns the
        number of records."""
        with self._lock:
            bodies = self.recorder.take(final, whole)
            if not self.recorder.pending:
                self._first, self._now = None, False
        if bodies:
            # The cast file first: a restart continues at its last offset, which then is never below a journaled one.
            self.cast.append([frame for body in bodies for frame in body["frames"]])
            with self.journal.transaction() as db:
                for body in bodies:
                    self.journal.insert(db, "session.terminal", body, self.session_id)
        return len(bodies)

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for data in the FIFO."""
        readable, _, _ = select.select([self._fd], [], [], max(0.0, timeout))
        return bool(readable)

    def _timeout(self) -> float:
        with self._lock:
            if self._now:
                return 0.0
            deadline = self._deadline()
        return self.limits.quiet_ms / 1000 if deadline is None else deadline - self.monotonic()

    def step(self) -> None:
        if self.wait(self._timeout()):
            self.drain()
        if self._full():
            self.flush(whole=True)
        if self._timed():
            self.flush()

    def start(self) -> None:
        self.open()
        self._thread = threading.Thread(target=self._run, name="agent-guest-recorder", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                sys.stderr.write("agent-guest-control: terminal capture failed\n")
                traceback.print_exc(file=sys.stderr)
                self._stop.wait(self.limits.quiet_ms / 1000)

    def stop(self) -> None:
        """Last read and flush; the pipe is closed and the FIFO removed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            if self._thread.is_alive():
                sys.stderr.write("agent-guest-control: terminal capture did not stop in time\n")
                return
        if self._fd < 0:
            return
        for _ in range(MAX_FINAL_DRAINS):
            if self.drain() < MAX_DRAIN:
                break
            self.flush()
        self.flush(final=True)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._tmux("pipe-pane", "-t", f"={tmux.session_name(self.session_id)}:")
        self._close()

    def _close(self) -> None:
        os.close(self._fd)
        self._fd = -1
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.fifo)
