"""How the runner reaches `limactl` for a work VM slot (docs/platform/spec/13-work-vm.md section 6.1).

A slot is a macOS account that owns one Lima home. In production the runner runs `limactl` for a slot only through
`sudo -n -u <account> <wrapper>`, where the root-owned wrapper (`accounts.wrapper_script`) sets the slot's `HOME`,
`LIMA_HOME` and `PATH` itself and executes `limactl` with the given arguments; the runner's own environment never
reaches the slot account. In `TEST_MODE` with `WORKVM_SLOTS_SELF` every slot is the runner's own account and its
`limactl` (no host filter: refused by the configuration outside `TEST_MODE`).

`SlotCommand.prefix(slot)` is the argv before `limactl`'s own arguments; `sync_runner(slot)` is the `LimactlRun` of
`agent_runner.vm.lima.Lima` for that slot. A slot whose home is missing (its volume hangs elsewhere after a restart,
HOT-25) has no Lima home, so no instance can exist there: `sync_runner` answers without running anything - `list`
with no instance, `delete` like `limactl` for a missing instance (so a deletion counts as done and the work VM task
never fails on it again and again), everything else as a failure (`slot home missing`). `VmShell` runs a command
inside a work VM (`limactl shell`) with bounded output, optional stdin (bytes or a file) and optional output into a
file.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_runner.vm.lima import EXIT_NOT_FOUND, EXIT_TIMEOUT, LimactlRun
from agent_runner.workvm.accounts import HOME_ROOT
from agent_runner.workvm.jobs import ShellResult

WRAPPER = "/usr/local/libexec/agent-runner-workvm-limactl"
CHUNK = 65536


@dataclass(frozen=True)
class SlotCommand:
    """The `limactl` argv prefix per slot: `accounts[slot]` through sudo and the wrapper, or `limactl` itself when
    `accounts` is empty (self mode)."""

    accounts: tuple[str, ...]
    limactl: str = "limactl"
    wrapper: str = WRAPPER
    home_root: str = HOME_ROOT
    has_home: Callable[[str], bool] = os.path.isdir

    def prefix(self, slot: int) -> list[str]:
        if not self.accounts:
            return [shutil.which(self.limactl) or self.limactl]
        return ["sudo", "-n", "-u", self.accounts[slot], self.wrapper]

    def sync_runner(self, slot: int) -> LimactlRun:
        prefix = self.prefix(slot)

        def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
            command = [*prefix, *argv]
            if self.accounts and not self.has_home(f"{self.home_root}/{self.accounts[slot]}"):
                return _without_home(command, argv)
            try:
                return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
            except FileNotFoundError:
                return subprocess.CompletedProcess(command, EXIT_NOT_FOUND, "", "limactl: not found")
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(command, EXIT_TIMEOUT, "", "limactl: timed out")

        return run


NO_HOME = "slot home missing"
IGNORED_MISSING = "Ignoring non-existent instance"
"""What `limactl delete -f` prints for a missing instance (`Lima.delete` then reports it as not there)."""


def _without_home(command: list[str], argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The answer for a slot without a home (module docstring)."""
    if argv[:1] == ["list"]:
        return subprocess.CompletedProcess(command, 0, "", "")
    if argv[:1] == ["delete"]:
        return subprocess.CompletedProcess(command, 0, "", f"{IGNORED_MISSING} {argv[-1]}: {NO_HOME}")
    return subprocess.CompletedProcess(command, 1, "", f"limactl: {NO_HOME}")


@dataclass(frozen=True)
class VmShell:
    """`limactl shell --workdir / <name> ARGV` of one work VM (the `jobs.Shell` protocol)."""

    command: SlotCommand
    slot: int
    lima_name: str

    def argv(self, argv: Sequence[str]) -> list[str]:
        """The full host argv that runs `argv` in the VM (also the long-lived screencast of the platform browser)."""
        return [*self.command.prefix(self.slot), "shell", "--workdir", "/", self.lima_name, *argv]

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | Path | None,
        timeout: float,
        limit: int,
        out: Path | None = None,
    ) -> ShellResult:
        process = await asyncio.create_subprocess_exec(
            *self.argv(argv),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return await asyncio.wait_for(self._talk(process, stdin, limit, out), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return ShellResult(EXIT_TIMEOUT, b"", False)

    async def _talk(
        self, process: asyncio.subprocess.Process, stdin: bytes | Path | None, limit: int, out: Path | None
    ) -> ShellResult:
        feeder = asyncio.ensure_future(_feed(process, stdin))
        assert process.stdout is not None
        kept = bytearray()
        size, truncated = 0, False
        sink = open(out, "wb") if out is not None else None  # noqa: SIM115 - closed below
        try:
            while chunk := await process.stdout.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    truncated = True
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    break
                if sink is not None:
                    sink.write(chunk)
                else:
                    kept += chunk
        finally:
            if sink is not None:
                sink.close()
        returncode = await process.wait()
        await feeder
        return ShellResult(returncode, bytes(kept), truncated)


async def _feed(process: asyncio.subprocess.Process, stdin: bytes | Path | None) -> None:
    if stdin is None or process.stdin is None:  # no stdin: the process got DEVNULL and has no pipe
        return
    try:
        if isinstance(stdin, Path):
            with open(stdin, "rb") as source:
                while chunk := source.read(CHUNK):
                    process.stdin.write(chunk)
                    await process.stdin.drain()
        else:
            process.stdin.write(stdin)
            await process.stdin.drain()
        process.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        return


DISK_NAMES = ("disk", "diffdisk")
"""The writable disk of an instance directory, first match wins: Lima 2.x keeps one `disk` (raw for `vz`, checked
with Lima 2.3.0), older Lima a `diffdisk` beside the `basedisk` of the image."""


def disk_path(lima_dir: str) -> str:
    """The writable disk of an instance directory (`DISK_NAMES`); `FileNotFoundError` naming `disk` without one."""
    for name in DISK_NAMES:
        path = os.path.join(lima_dir, name)
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(errno.ENOENT, "no disk in the Lima instance directory", os.path.join(lima_dir, "disk"))
