"""U-RUN-vnc (screen): the VNC server of a VM's screen through `workvm-vnc` (ADR-103 section 2) - the one-time password,
the untrusted answer of `start`, the long-lived `connect`, `stop`, the end of a connection - and the screens offered
to their owner (`agent_runner.vnc.screens`)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.vnc import screen as screen_module
from agent_runner.vnc.screen import ALPHABET, PASSWORD_LENGTH, Local, ScreenError, VmScreen, argv, one_time_password
from agent_runner.vnc.screens import Screens, screens_for
from agent_runner.workvm.jobs import ShellResult


class Shell:
    def __init__(self, *results: ShellResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], bytes | Path | None]] = []

    async def __call__(
        self, command: Sequence[str], *, stdin: bytes | Path | None, timeout: float, limit: int
    ) -> ShellResult:
        self.calls.append((list(command), stdin))
        return self.results.pop(0)


def test_the_password_is_fresh_letters_and_digits() -> None:
    first, second = one_time_password(), one_time_password()
    assert len(first) == PASSWORD_LENGTH
    assert set(first) <= set(ALPHABET)
    assert first != second


def test_start_sends_the_password_on_stdin_and_needs_the_closed_answer() -> None:
    async def main() -> None:
        shell = Shell(ShellResult(0, b'{"ok":true}\n', False))
        screen = VmScreen(shell, spawn=None)  # type: ignore[arg-type]
        await screen.start("Ab3dEf7h")
        assert shell.calls == [(["sudo", "-n", "/usr/local/bin/workvm-vnc", "start"], b"Ab3dEf7h\n")]
        for result in (
            ShellResult(1, b'{"ok":true}', False),
            ShellResult(0, b'{"ok":true}', True),
            ShellResult(0, b'{"ok":false}', False),
            ShellResult(0, b"not json", False),
            ShellResult(0, b"\xff\xfe", False),
        ):
            with pytest.raises(ScreenError) as failed:
                await VmScreen(Shell(result), spawn=None).start("Ab3dEf7h")  # type: ignore[arg-type]
            assert failed.value.reason == "vnc_start_failed"
        for wrong in ("short", "Ab3dEf7h9", "Ab3dEf7!"):
            with pytest.raises(ValueError, match="password form"):
                await screen.start(wrong)

    asyncio.run(main())


class Process:
    def __init__(self, *, ends: bool = True, gone: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdin = Writer()
        self.ends = ends
        self.gone = gone
        self.killed = False

    def kill(self) -> None:
        if self.gone:
            raise ProcessLookupError
        self.killed = True

    async def wait(self) -> int:
        if not self.ends:
            await asyncio.sleep(3600)
        return 0


class Writer:
    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.fail = fail

    def close(self) -> None:
        if self.fail:
            raise OSError
        self.closed = True


def test_connect_starts_the_helper_and_close_ends_it_on_every_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        process = Process()
        spawned: list[list[str]] = []

        async def spawn(command: Sequence[str]) -> Any:
            spawned.append(list(command))
            return process

        shell = Shell(ShellResult(0, b"", False))
        screen = VmScreen(shell, spawn)
        local = await screen.connect()
        assert spawned == [argv("connect")]
        assert (local.reader, local.writer, local.process) == (process.stdout, process.stdin, process)
        await local.close()
        assert process.stdin.closed
        assert process.killed
        await screen.stop()
        assert shell.calls[-1] == (argv("stop"), None)
        monkeypatch.setattr(screen_module, "END_WAIT", 0.05)
        stuck = Process(ends=False, gone=True)
        stuck.stdin = Writer(fail=True)
        await Local(stuck.stdout, stuck.stdin, stuck).close()  # a helper that hangs is left after END_WAIT
        plain = Writer()
        await Local(asyncio.StreamReader(), plain, None).close()
        assert plain.closed

    asyncio.run(main())


def test_screens_are_found_by_their_owner_only_and_end_when_withdrawn() -> None:
    async def main() -> None:
        context = SimpleNamespace()
        screens = screens_for(context)
        assert screens_for(context) is screens
        first = screens.offer("login", "r1", "alice", screen=None)  # type: ignore[arg-type]
        assert screens.find("login", "r1", "alice") is first
        assert screens.find("login", "r1", "bob") is None
        assert screens.find("session", "r1", "alice") is None
        second = screens.offer("login", "r1", "alice", screen=None)  # type: ignore[arg-type]
        assert first.ended.is_set()
        assert not second.ended.is_set()
        screens.withdraw(first)  # an old offer leaves the newer one in place
        assert screens.find("login", "r1", "alice") is second
        screens.withdraw(second)
        assert second.ended.is_set()
        assert screens.offers == {}

    asyncio.run(main())


def test_a_screen_error_names_its_reason() -> None:
    assert ScreenError("vnc_start_failed").reason == "vnc_start_failed"
    assert isinstance(Screens(), Screens)
