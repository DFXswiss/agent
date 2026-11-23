"""Setup-token relay of the Claude subscription login (05-runner.md section 11.2, ADR-011).

`LoginSession` runs `LOGIN_ARGV` (default `claude setup-token`) for one `claude.login` request:

1. `start` creates `$AGENT_RUNNER_HOME/tmp/login-<request id>` (0700) with its own `HOME`, `CLAUDE_CONFIG_DIR` and
   `TMPDIR` below it, writes the `BROWSER` capture helper (`capture.py`) and starts the command in a PTY (`pty.py`)
   with an allow-list environment built from scratch (`login_env`): nothing of the runner's environment is inherited,
   so existing subscription profiles (`~/.claude`, `~/.local/share/agent/providers/…`, keychain names) are never
   read or written. It returns the authorize URL the command handed to `BROWSER` within `url_wait` seconds.
2. `finish(code)` types the authorization code, reads the terminal until the command ends (`token_wait`), and
   returns the long-lived token of the token block (`parse_token`: ANSI sequences removed, a hard-wrapped token
   joined, format checked).
3. `close` kills the command's process group and removes the temporary directory; it is idempotent and runs on every
   path (success, failure, cancellation by the request service).

Failures raise `LoginFailed(code)` with a code of `contracts/requests/objects.json`: `timeout` (no URL or no end of the
command in time), `login_cli_failed` (cannot start, exits early, an unusable URL), `code_invalid` (the command
refused the code: `OAuth error`), `token_invalid` (no token of the expected format). The URL, the code and the token
are never logged.
"""

from __future__ import annotations

import imaplib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.identity import ensure_private_dir
from agent_runner.login import capture
from agent_runner.login.pty import PtyProcess, SpawnFailed

URL_POLL = 0.1
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
TOKEN_MARKER = "OAuth token"
CLI_ERROR = "OAuth error"
TOKEN_PREFIX = "sk-" + "ant-oat"
TOKEN_RE = re.compile(re.escape(TOKEN_PREFIX) + r"[0-9]{2}-[A-Za-z0-9_-]{16,4000}")
"""A long-lived setup token (`sk-ant-oatNN-…`, at most 4096 characters as the credential contract allows)."""
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


class LoginFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LoginOptions:
    """Time limits and seams of the login (tests replace them through `context.login_options`)."""

    url_wait: float = 30.0
    """Seconds until the command must have opened the authorize URL (section 11.2 step 2)."""
    token_wait: float = 60.0
    """Seconds after the code until the command must have ended."""
    code_margin: float = 5.0
    """Seconds before the request's expiry at which waiting for the user's code ends."""
    page_timeout: float = 30.0
    """Seconds per page step of the mailbox automation."""
    mail_poll: float = 5.0
    mail_wait: float = 600.0
    mail_from: str = "anthropic.com"
    """Sender filter of the magic-link mail (IMAP `SEARCH FROM`, a substring of the sender)."""
    imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL
    playwright: Callable[[], Callable[[], Any]] | None = None
    """Returns `playwright.async_api.async_playwright` (None: import it from the `login` extra)."""


def options(context: Any) -> LoginOptions:
    found = getattr(context, "login_options", None)
    return found if isinstance(found, LoginOptions) else LoginOptions()


def resolve(argv: tuple[str, ...], path: str) -> list[str]:
    """`argv` with an absolute program (looked up in the runner's `PATH`); `LoginFailed` when it does not exist."""
    program = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0], path=path)
    if program is None or not os.access(program, os.X_OK):
        raise LoginFailed("login_cli_failed")
    return [program, *argv[1:]]


def login_env(workdir: Path, program: str) -> dict[str, str]:
    """The complete environment of the login command: paths only below `workdir`, the program's directory and the
    system directories in `PATH`; nothing inherited."""
    search = [os.path.dirname(program), *SYSTEM_PATH]
    return {
        "PATH": os.pathsep.join(dict.fromkeys(search)),
        "HOME": str(workdir / "home"),
        "CLAUDE_CONFIG_DIR": str(workdir / "config"),
        "TMPDIR": str(workdir / "tmp"),
        "BROWSER": str(workdir / capture.HELPER),
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "COLUMNS": str(1000),
        "NO_COLOR": "1",
    }


def clean(text: str) -> str:
    """Terminal output without ANSI sequences and with plain line ends."""
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def parse_token(text: str) -> str | None:
    """The token of the token block: the non-empty lines after the marker line up to the next empty line, joined (a
    narrow terminal hard-wraps the token); None when there is no block or it does not hold a token."""
    lines = [line.strip() for line in clean(text).split("\n")]
    marker = next((i for i, line in enumerate(lines) if TOKEN_MARKER in line), None)
    if marker is None:
        return None
    rest = lines[marker + 1 :]
    while rest and not rest[0]:
        rest.pop(0)
    parts: list[str] = []
    for line in rest:
        if not line:
            break
        parts.append(line)
    token = "".join(parts)
    return token if TOKEN_RE.fullmatch(token) else None


class LoginSession:
    """One run of the login command (see the module docstring)."""

    def __init__(self, tmp_dir: Path, request_id: str, argv: tuple[str, ...], search_path: str) -> None:
        self.tmp_dir = tmp_dir
        self.workdir = tmp_dir / f"login-{request_id}"
        self.argv = argv
        self.search_path = search_path
        self.process: PtyProcess | None = None
        self.left = False
        """True when the temporary directory could not be removed completely (logged by the caller)."""

    async def start(self, opts: LoginOptions) -> str:
        """Start the command; the authorize URL."""
        ensure_private_dir(self.tmp_dir)
        os.mkdir(self.workdir, 0o700)
        for sub in ("home", "config", "tmp"):
            os.mkdir(self.workdir / sub, 0o700)
        capture.write_helper(self.workdir)
        argv = resolve(self.argv, self.search_path)
        env = login_env(self.workdir, argv[0])
        try:
            self.process = PtyProcess.spawn(argv, env, self.workdir)
        except SpawnFailed:
            raise LoginFailed("login_cli_failed") from None
        return await self._url(self.process, opts.url_wait)

    async def _url(self, process: PtyProcess, seconds: float) -> str:
        deadline = process.loop.time() + seconds
        while True:
            try:
                url = capture.captured(self.workdir)
            except capture.UrlInvalid:
                raise LoginFailed("login_cli_failed") from None
            if url is not None:
                return url
            if not process.running():
                raise LoginFailed("login_cli_failed")
            remaining = deadline - process.loop.time()
            if remaining <= 0:
                raise LoginFailed("timeout")
            await process.wait_change(min(remaining, URL_POLL))

    async def finish(self, code: str, opts: LoginOptions) -> str:
        """Type the code; the token."""
        process = self.process
        assert process is not None
        try:
            process.write(code + "\r")
        except OSError:
            raise LoginFailed("login_cli_failed") from None
        if not await process.wait_eof(opts.token_wait):
            raise LoginFailed("timeout")
        status = await process.reap()
        text = process.text()
        if status != 0:
            raise LoginFailed("code_invalid" if CLI_ERROR in clean(text) else "login_cli_failed")
        token = parse_token(text)
        if token is None:
            raise LoginFailed("token_invalid")
        return token

    def close(self) -> None:
        """Kill the command and remove the temporary directory (idempotent)."""
        if self.process is not None:
            self.process.kill()
            self.process = None
        shutil.rmtree(self.workdir, ignore_errors=True)
        if self.workdir.exists():
            self.left = True
