"""What the installation helper takes from the Mac, injectable for tests (pattern of `agent_runner.host`).

Every external program runs through `run` with its full argv (list form, never a shell, output captured, never
raising on a non-zero exit); `fetch` reads an HTTPS URL with a size limit. Tests replace them with recording fakes
and point `layout.root` at a temporary directory.
"""

from __future__ import annotations

import os
import platform as platform_module
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from agent_runner.errors import RunnerError
from agent_runner.install.layout import Layout

RUN_TIMEOUT_S = 3600.0
FETCH_TIMEOUT_S = 60.0
FETCH_CHUNK = 1024 * 1024

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
InputRunner = Callable[[Sequence[str], str], "subprocess.CompletedProcess[str]"]
Fetcher = Callable[[str, int], bytes]


def run_argv(argv: Sequence[str], timeout: float = RUN_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run `argv` without a shell; a missing program is exit 127, a timeout exit 124."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", "not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "timed out")


def run_with_input(argv: Sequence[str], text: str) -> subprocess.CompletedProcess[str]:
    """`run_argv` with `text` on standard input (a secret never put into an argv or a file)."""
    try:
        return subprocess.run(
            list(argv), input=text, capture_output=True, text=True, check=False, timeout=RUN_TIMEOUT_S
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(list(argv), 127, "", "not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", "timed out")


def fetch_url(url: str, limit: int) -> bytes:
    """GET `url` (https, or loopback http in tests) and return at most `limit` bytes; `RunnerError` otherwise."""
    if not url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise RunnerError("download_failed", "scheme")
    request = urllib.request.Request(url, headers={"User-Agent": "agent-runner-install"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            chunks: list[bytes] = []
            size = 0
            while chunk := response.read(FETCH_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise RunnerError("download_failed", "size")
                chunks.append(chunk)
    except OSError:
        raise RunnerError("download_failed") from None
    return b"".join(chunks)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class System:
    layout: Layout = field(default_factory=Layout)
    run: Runner = run_argv
    run_input: InputRunner = run_with_input
    fetch: Fetcher = fetch_url
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    platform: str = sys.platform
    machine: Callable[[], str] = platform_module.machine
    geteuid: Callable[[], int] = os.geteuid
    disk_free: Callable[[Path], int] = free_bytes
    now: Callable[[], datetime] = utc_now
    local_hour: Callable[[], int] = lambda: time.localtime().tm_hour
    root_uid: int = 0
    chown: Callable[[Path, int, int], None] = lambda path, uid, gid: os.chown(path, uid, gid, follow_symlinks=False)

    def must(self, argv: Sequence[str], code: str, name: str | None = None) -> str:
        """Run `argv`; its stdout, or `RunnerError(code, name)` on a non-zero exit."""
        done = self.run(argv)
        if done.returncode != 0:
            raise RunnerError(code, name)
        return done.stdout
