"""U-RUN-login (units): the capture helper and the PTY wrapper of the relay (the screen and its parser:
`test_screen.py`, `test_setup_token.py`)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_runner.login import capture, pty, relay
from agent_runner.login.pty import PtyProcess, SpawnFailed


def test_the_capture_helper_writes_the_url_atomically(tmp_path: Path) -> None:
    helper = capture.write_helper(tmp_path)
    assert helper.stat().st_mode & 0o777 == 0o700
    subprocess.run([str(helper), "https://auth.example/oauth/authorize?a=1"], check=True)
    assert (tmp_path / "url.txt").read_text() == "https://auth.example/oauth/authorize?a=1"
    assert not (tmp_path / "url.txt.partial").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["capture-url", "url.txt"]
    with pytest.raises(FileExistsError):
        capture.write_helper(tmp_path)


def test_a_relative_program_is_looked_up_and_a_missing_one_refused(tmp_path: Path) -> None:
    program = tmp_path / "login-cli"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o700)
    assert relay.resolve(("login-cli", "setup-token"), str(tmp_path)) == [str(program), "setup-token"]
    plain = tmp_path / "plain"
    plain.write_text("")
    for argv in (("missing-cli",), (str(plain),)):
        with pytest.raises(relay.LoginFailed) as failed:
            relay.resolve(argv, str(tmp_path))
        assert failed.value.code == "login_cli_failed"


def test_the_default_options_are_used_without_test_options() -> None:
    assert relay.options(object()) == relay.LoginOptions()


def test_pty_reads_until_eof_before_reaping(tmp_path: Path) -> None:
    async def main() -> None:
        process = PtyProcess.spawn(
            [sys.executable, "-c", "import sys; print(input('> ') * 2); sys.exit(4)"], {}, tmp_path
        )
        process.write("ab\r")
        assert await process.wait_eof(20)
        assert await process.reap() == 4
        assert "abab" in process.text()
        assert "> abab" in process.screen.lines()
        process.kill()
        process.kill()

    asyncio.run(main())


def test_pty_output_is_bounded_and_a_slow_end_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pty, "MAX_OUTPUT", 1000)
    monkeypatch.setattr(pty, "REAP_WAIT", 0.1)

    async def main() -> None:
        source = "import sys, time; sys.stdout.write('x' * 5000 + 'END'); sys.stdout.flush(); time.sleep(30)"
        process = PtyProcess.spawn([sys.executable, "-c", source], {}, tmp_path)
        while "END" not in process.text():
            await process.wait_change(5)
        assert len(process.output) <= 1000
        assert not await process.wait_eof(0.2)
        assert process.running()
        assert await process.reap() == -9
        process.kill()

    asyncio.run(main())


def test_a_spurious_wakeup_of_the_reader_is_ignored(tmp_path: Path) -> None:
    async def main() -> None:
        process = PtyProcess.spawn([sys.executable, "-c", "import time; time.sleep(30)"], {}, tmp_path)
        process._readable()
        assert process.output == bytearray()
        assert not process.eof
        process.kill()

    asyncio.run(main())


def test_a_read_error_counts_as_end_of_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> None:
        process = PtyProcess.spawn([sys.executable, "-c", "import time; time.sleep(30)"], {}, tmp_path)

        def broken(_fd: int, _n: int) -> bytes:
            raise OSError(5, "EIO")

        monkeypatch.setattr(os, "read", broken)
        process._readable()
        monkeypatch.undo()
        assert process.eof
        process.kill()

    asyncio.run(main())


def test_a_program_that_cannot_start_raises_spawn_failed(tmp_path: Path) -> None:
    async def main() -> None:
        with pytest.raises(SpawnFailed):
            PtyProcess.spawn([str(tmp_path / "missing")], {}, tmp_path)

    asyncio.run(main())


def test_the_window_size_and_utf_8_split_between_reads_reach_the_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pty, "ROWS", 7)
    monkeypatch.setattr(pty, "COLUMNS", 33)
    source = (
        "import os, shutil, sys, time; size = shutil.get_terminal_size(); data = '✘ OAuth'.encode(); "
        "os.write(1, f'{size.lines}x{size.columns} '.encode() + data[:2]); time.sleep(0.3); os.write(1, data[2:])"
    )

    async def main() -> None:
        process = PtyProcess.spawn([sys.executable, "-c", source], {}, tmp_path)
        assert await process.wait_eof(20)
        assert (process.screen.rows, process.screen.columns) == (7, 33)
        assert process.screen.lines()[0] == "7x33 ✘ OAuth"
        await process.reap()
        process.kill()

    asyncio.run(main())


def test_typing_into_a_closed_terminal_is_login_cli_failed(tmp_path: Path) -> None:
    async def main() -> None:
        session = relay.LoginSession(tmp_path / "tmp", "r1", (sys.executable, "-c", "pass"), "")
        session.workdir.parent.mkdir(mode=0o700)
        session.workdir.mkdir(mode=0o700)
        source = "import time; print('Paste code here if prompted', flush=True); time.sleep(30)"
        session.process = PtyProcess.spawn([sys.executable, "-c", source], {}, session.workdir)
        while "Paste code" not in session.process.text():
            await session.process.wait_change(5)
        session.process.close()
        with pytest.raises(relay.LoginFailed) as failed:
            await session.finish("code", relay.LoginOptions())
        assert failed.value.code == "login_cli_failed"
        session.close()
        assert not session.left

    asyncio.run(main())


def test_an_incomplete_cleanup_is_reported(tmp_path: Path) -> None:
    session = relay.LoginSession(tmp_path, "r2", ("x",), "")
    locked = session.workdir / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    locked.chmod(0o500)
    try:
        session.close()
        assert session.left
    finally:
        locked.chmod(0o700)
