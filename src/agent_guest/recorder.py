"""Terminal recorder of the control process (05-runner.md section 7 and 4.6 defects 5 and 6; ADR-016).

The pane of the tmux session ``s-<id>`` is piped into a FIFO with ``tmux pipe-pane -O`` (the tmux server runs
``cat >> <fifo>`` as root); the recorder thread of the control process reads the FIFO. The FIFO lives in the
root-only directory ``FIFO_DIR`` and is opened read-write, so it never reports end-of-file while ``cat`` is replaced.

``Recorder`` (pure) turns bytes into asciicast v2 output events:

* an incremental UTF-8 decoder (``errors="replace"``) keeps a character split across reads for the next read
* events ``[t_ms, "o", data]`` with ``t_ms`` in milliseconds since the session's recording started, never
  decreasing; a restart of the recorder (control process restarted, session resumed) continues at the last written
  offset and writes a marker ``[t_ms, "m", "recorder restarted"]`` instead of a new header
* ``take()`` groups the buffered events into ``session.terminal {offset_ms_start, frames, cols, rows}`` bodies of at
  most ``MAX_RECORD`` UTF-8 bytes of data (an event longer than that is split), ``MAX_FRAMES`` events and
  ``MAX_BODY_JSON`` bytes of body JSON: the runner encrypts exactly that JSON, and JSON escapes a control character
  as six bytes (``\u001b``), so the raw data alone does not bound the sealed event (E8: 64 KiB, 04-crypto.md
  section 6.4; decisions/KOR-02.md)

``TerminalRecorder`` (one per session, a thread): flushes every ``FLUSH_MS`` after the first buffered event or as
soon as ``MAX_RECORD`` bytes are buffered, journals the bodies and appends the same events to the session's
asciicast v2 file ``<session base>/terminal.cast`` (root 0600; times in seconds = ``t_ms / 1000``), the copy the
history is compared with (E2E-12). The first output of the pane calls ``on_output`` (the "TUI up" signal).
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

FLUSH_MS = 100
MAX_RECORD = 16 * 1024
AEAD_TAG_BYTES = 16
MAX_BODY_JSON = 64 * 1024 - AEAD_TAG_BYTES
"""Body JSON of one record: the sealed ``session.terminal`` (body JSON plus the 16-byte tag) stays within E8."""
MAX_PIECE_JSON = MAX_RECORD
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


def split(text: str, limit: int = MAX_RECORD, json_limit: int = MAX_PIECE_JSON) -> list[str]:
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
    def __init__(self, base_ms: int = 0, cols: int = tmux.COLS, rows: int = tmux.ROWS) -> None:
        self.cols = cols
        self.rows = rows
        self.last_ms = base_ms
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._frames: list[Frame] = []
        self._bytes = 0

    @property
    def pending_bytes(self) -> int:
        return self._bytes

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
            self._frames.append([self.last_ms, code, piece])
            self._bytes += len(piece.encode("utf-8"))

    def take(self, final: bool = False) -> list[dict[str, Any]]:
        """The buffered events as journal bodies; ``final`` also ends a character left incomplete."""
        if final:
            self._add(self.last_ms, "o", self._decoder.decode(b"", final=True))
        bodies: list[dict[str, Any]] = []
        group: list[Frame] = []
        size = encoded = 0
        for frame in self._frames:
            length = len(frame[2].encode("utf-8"))
            frame_json = json_size(frame)
            if group and (
                size + length > MAX_RECORD or len(group) == MAX_FRAMES or encoded + 1 + frame_json > MAX_BODY_JSON
            ):
                bodies.append(self._body(group))
                group, size = [], 0
            if not group:
                encoded = json_size(self._body([frame]))
            else:
                encoded += 1 + frame_json
            group.append(frame)
            size += length
        if group:
            bodies.append(self._body(group))
        self._frames, self._bytes = [], 0
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
    ) -> None:
        self.journal = journal
        self.session_id = session_id
        self.terminal = terminal
        self.run = run
        self.fifo = f"{fifo_dir}/{session_id}.fifo"
        self.cast = CastFile(f"{base}/{CAST_NAME}")
        self.monotonic = monotonic
        self.on_output = on_output
        self.recorder = Recorder()
        self._fd = -1
        self._started = 0.0
        self._base_ms = 0
        self._due: float | None = None
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
        self.recorder = Recorder(base_ms)
        if restarted:
            self.recorder.marker(base_ms)
            self._due = self.monotonic()
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
                if self._due is None:
                    self._due = self.monotonic() + FLUSH_MS / 1000
            if len(data) < READ_CHUNK or total >= MAX_DRAIN:
                # The FIFO is open for writing as well, so it never reads as ended; a short read means empty for now.
                # A bounded amount per call keeps flushes (16 KiB) and the stop check going under constant output.
                break
        if total and not self._seen_output:
            self._seen_output = True
            if self.on_output is not None:
                self.on_output()
        return total

    def due(self) -> bool:
        with self._lock:
            if not self.recorder.pending:
                return False
            return self.recorder.pending_bytes >= MAX_RECORD or (
                self._due is not None and self.monotonic() >= self._due
            )

    def flush(self, final: bool = False) -> int:
        """Journal the buffered events and append them to the cast file; returns the number of records."""
        with self._lock:
            bodies = self.recorder.take(final)
            self._due = None
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
            due = self._due
        return FLUSH_MS / 1000 if due is None else due - self.monotonic()

    def step(self) -> None:
        if self.wait(self._timeout()):
            self.drain()
        if self.due():
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
                self._stop.wait(FLUSH_MS / 1000)

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
