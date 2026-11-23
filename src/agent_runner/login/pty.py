"""A command in a pseudo-terminal, driven from asyncio (05-runner.md section 11.2 step 2).

`claude setup-token` is an interactive terminal program: it needs a TTY for its prompt and prints the token in its
terminal UI. `PtyProcess.spawn` starts the command as the leader of a new session with the PTY's slave side as
standard input, output and error and a wide window (`COLUMNS` x `ROWS`), so the UI wraps as little as possible. The
master side is read by an event-loop reader into a bounded buffer and, decoded incrementally, into a `Screen` of the
window's size (KOR-21: the program draws with cursor positioning, so the login reads the screen, not the raw output);
`write` types into the terminal.

On macOS the output that is still buffered in the PTY is lost once the child is reaped, so `wait_eof` reads the master
side until end of file before `reap` collects the exit status (DBL-03 report). `kill` ends the whole process group
(the command and every helper it started) and is synchronous, so it also runs while a flow is being cancelled.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import os
import signal
import struct
import subprocess
import termios
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_runner.login.screen import Screen

COLUMNS = 1000
ROWS = 50
MAX_OUTPUT = 1024 * 1024
"""Bytes of terminal output kept (the oldest are dropped beyond)."""
CHUNK = 65536
REAP_WAIT = 5.0


class SpawnFailed(Exception):
    """The command could not be started."""


class PtyProcess:
    def __init__(self, process: subprocess.Popen[bytes], master: int, rows: int, columns: int) -> None:
        self.process = process
        self.master = master
        self.output = bytearray()
        self.screen = Screen(rows, columns)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.eof = False
        self.changed = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(master, self._readable)

    @classmethod
    def spawn(cls, argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> PtyProcess:
        """Start `argv` with exactly `env` in `cwd` in a `ROWS` x `COLUMNS` window; `SpawnFailed` when it cannot be
        started."""
        rows, columns = ROWS, COLUMNS
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            process = subprocess.Popen(
                list(argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=dict(env),
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            os.close(master)
            raise SpawnFailed from None
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        return cls(process, master, rows, columns)

    def _readable(self) -> None:
        try:
            chunk = os.read(self.master, CHUNK)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if chunk:
            self.output += chunk
            self.screen.feed(self._decoder.decode(chunk))
            if len(self.output) > MAX_OUTPUT:
                del self.output[: len(self.output) - MAX_OUTPUT]
        else:
            self.eof = True
            self.loop.remove_reader(self.master)
        self.changed.set()

    def text(self) -> str:
        return self.output.decode("utf-8", "replace")

    def write(self, text: str) -> None:
        """Type `text` into the terminal."""
        data = text.encode()
        while data:
            written = os.write(self.master, data)
            data = data[written:]

    def running(self) -> bool:
        return self.process.poll() is None

    async def wait_change(self, seconds: float) -> None:
        """Return after new output or end of file, at the latest after `seconds`."""
        self.changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.changed.wait(), timeout=max(seconds, 0.0))

    async def wait_eof(self, seconds: float) -> bool:
        """Read until end of file (the command and its helpers closed the terminal); False after `seconds`."""
        deadline = self.loop.time() + seconds
        while not self.eof:
            remaining = deadline - self.loop.time()
            if remaining <= 0:
                return False
            await self.wait_change(remaining)
        return True

    async def reap(self) -> int:
        """The exit status, waited for off the event loop (the process group is killed when the command does not end
        within `REAP_WAIT` seconds)."""
        try:
            return await asyncio.to_thread(self.process.wait, REAP_WAIT)
        except subprocess.TimeoutExpired:
            self.kill()
            return self.process.wait()

    def kill(self) -> None:
        """Kill the process group (while the command has not been reaped, so its ID cannot have been reused) and
        release the terminal; idempotent."""
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        self.close()

    def close(self) -> None:
        if self.master < 0:
            return
        if not self.eof:
            self.loop.remove_reader(self.master)
        os.close(self.master)
        self.master = -1
