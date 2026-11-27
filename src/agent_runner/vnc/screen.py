"""The screen of a sandbox and its VNC server (ADR-103 section 2).

A sandbox with a screen is a VM whose browser draws into a display (the platform browser helper runs Chromium with a
window on the VM's own X display, `workvm-browser`). The helper `workvm-vnc` of the work image serves that display
over RFB on the VM's loopback only:

- `start` reads a one-time password (`PASSWORD_LENGTH` letters and digits; classic VNC authentication uses at most
  eight characters) on stdin and starts the VNC server for exactly that password; it answers `{"ok":true}`;
- `connect` joins its stdin and stdout with the server's port (one long-lived process per viewer);
- `stop` ends the server and removes the password file.

The runner reaches the helper over the slot's `limactl shell` like `workvm-browser`, and treats every answer as
untrusted (bounded output, closed answer). The password is never logged and never written outside the VM's
`/run`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import string
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_runner.workvm.jobs import ShellResult

HELPER = "/usr/local/bin/workvm-vnc"
PASSWORD_LENGTH = 8
ALPHABET = string.ascii_letters + string.digits
START_TIMEOUT = 20.0
STOP_TIMEOUT = 10.0
ANSWER_LIMIT = 4096
END_WAIT = 5.0


class ScreenError(Exception):
    """The VNC server of a screen could not be started (`reason`, a short word for the log)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def one_time_password() -> str:
    """A fresh password for one connection."""
    return "".join(secrets.choice(ALPHABET) for _ in range(PASSWORD_LENGTH))


def argv(command: str) -> list[str]:
    return ["sudo", "-n", HELPER, command]


@dataclass
class Local:
    """The local end of one viewer's connection: the helper's stdout to read, its stdin to write."""

    reader: asyncio.StreamReader
    writer: Any
    process: Any

    async def close(self) -> None:
        """Close the helper's stdin (it ends with it), kill it when it does not, wait for it (a connection without a
        helper process, a double's, only closes)."""
        with contextlib.suppress(OSError):
            self.writer.close()
        if self.process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.process.wait()), END_WAIT)


class Screen(Protocol):
    async def start(self, password: str) -> None: ...

    async def connect(self) -> Local: ...

    async def stop(self) -> None: ...


Shell = Callable[..., Awaitable[ShellResult]]
Spawn = Callable[[Sequence[str]], Awaitable[Any]]


class VmScreen:
    """The screen of a VM: `shell` runs one command in it (`workvm.shell.VmShell`), `spawn` starts a long-lived
    process with stdin and stdout pipes (`workvm.browser_live.spawn_process`)."""

    def __init__(self, shell: Shell, spawn: Spawn) -> None:
        self.shell = shell
        self.spawn = spawn

    async def start(self, password: str) -> None:
        if len(password) != PASSWORD_LENGTH or any(char not in ALPHABET for char in password):
            raise ValueError("password form")
        result = await self.shell(
            argv("start"), stdin=(password + "\n").encode(), timeout=START_TIMEOUT, limit=ANSWER_LIMIT
        )
        if result.returncode != 0 or result.truncated or _answer(result.stdout) != {"ok": True}:
            raise ScreenError("vnc_start_failed")

    async def connect(self) -> Local:
        process = await self.spawn(argv("connect"))
        return Local(process.stdout, process.stdin, process)

    async def stop(self) -> None:
        await self.shell(argv("stop"), stdin=None, timeout=STOP_TIMEOUT, limit=ANSWER_LIMIT)


def _answer(stdout: bytes) -> object:
    try:
        return json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
