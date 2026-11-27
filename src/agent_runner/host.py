"""Everything the runner takes from its host, injectable for tests (legacy injection style, 07-testing.md section 1).

External tools (`launchctl`, `limactl`, `tmux`, `ssh`, `sudo`) are passed as callables that take an argv list and
return a `CompletedProcess`; the defaults run the real binaries. Tests replace them with recording fakes, the
environment with a dict, the platform with a string, the clock and sleep with fakes, the discovery root with a test
package and the account functions (effective uid, account lookup, chown) with fakes.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import pwd
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from agent_runner.registry import ROOT_PACKAGE
from agent_runner.store.cluster import ClusterTools, device_tools

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
COMMAND_TIMEOUT_S = 120.0
LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run `argv` (list form, never a shell), capture text output, never raise on a non-zero exit."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT_S)
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"{argv[0]}: timed out")


def tool(name: str) -> CommandRunner:
    """A runner for the external tool `name`: the argv given excludes the tool name."""

    def call(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return run_command([name, *argv])

    return call


def cli_program() -> list[str]:
    """Argv prefix of this installation's CLI (the interpreter of this process)."""
    return [sys.executable, "-m", "agent_runner"]


def daemon_program() -> list[str]:
    """Argv that starts the daemon from this installation."""
    return [*cli_program(), "run"]


@dataclass(frozen=True)
class Account:
    uid: int
    gid: int
    home: Path


def lookup_account(name: str) -> Account | None:
    """The OS account `name` (uid, primary gid, home directory), None when it does not exist."""
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    return Account(uid=entry.pw_uid, gid=entry.pw_gid, home=Path(entry.pw_dir))


def chown(path: Path, uid: int, gid: int) -> None:
    """Change the owner of `path` itself (never the target of a symbolic link)."""
    os.chown(path, uid, gid, follow_symlinks=False)


@dataclass
class Host:
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    platform: str = sys.platform
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    launchctl: CommandRunner = field(default_factory=lambda: tool("launchctl"))
    limactl: CommandRunner = field(default_factory=lambda: tool("limactl"))
    tmux: CommandRunner = field(default_factory=lambda: tool("tmux"))
    ssh: CommandRunner = field(default_factory=lambda: tool("ssh"))
    cluster: ClusterTools = field(default_factory=device_tools)
    package: str = ROOT_PACKAGE
    launch_daemons_dir: Path = LAUNCH_DAEMONS_DIR
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    install_signals: bool = True
    user: Callable[[], str] = getpass.getuser
    program: Callable[[], list[str]] = daemon_program
    cli: Callable[[], list[str]] = cli_program
    sudo: CommandRunner = field(default_factory=lambda: tool("sudo"))
    geteuid: Callable[[], int] = os.geteuid
    account: Callable[[str], Account | None] = lookup_account
    chown: Callable[[Path, int, int], None] = chown

    @property
    def under_pytest(self) -> bool:
        """True inside a pytest run: no launchctl, the plist goes into the test home (pattern of agent_cli.daemon)."""
        return "PYTEST_CURRENT_TEST" in self.env
