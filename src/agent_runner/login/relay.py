"""Setup-token relay of the Claude subscription login (05-runner.md section 11.2, ADR-011, ADR-051).

`LoginSession` runs `LOGIN_ARGV` (default `claude setup-token`) for one `claude.login` request:

1. `start` creates `$AGENT_RUNNER_HOME/tmp/login-<request id>` (0700) with its own `HOME`, `CLAUDE_CONFIG_DIR` and
   `TMPDIR` below it, writes the `BROWSER` capture helper (`capture.py`) and starts the command in a PTY (`pty.py`)
   with an allow-list environment built from scratch (`login_env`): nothing of the runner's environment is inherited,
   so existing subscription profiles (`~/.claude`, `~/.local/share/agent/providers/…`, keychain names) are never
   read or written. It returns the authorize URL the command handed to `BROWSER` within `url_wait` seconds.
2. `finish(code)` waits until the input field is on the screen (`prompt_wait`; the program shows it a few seconds
   after the URL and drops what is typed before), types the authorization code and watches the screen: the error
   message ends the login at once with `code_invalid` (the program stays open with "Press Enter to retry"); otherwise
   the command must end within `token_wait`, and the token is read from its last screen (`setup_token.parse_token`).
3. `close` kills the command's process group and removes the temporary directory; it is idempotent and runs on every
   path (success, failure, cancellation by the request service).

The login reads the reconstructed screen of the PTY (`screen.Screen`), never the raw output: the program places words
with cursor positioning instead of spaces (KOR-21). Failures raise `LoginFailed(code)` with a code of
`contracts/requests/objects.json`: `timeout` (no URL, no input field or no end of the command in time),
`login_cli_failed` (cannot start, exits early, an unusable URL), `code_invalid` (the command refused the code:
`OAuth error`), `token_invalid` (no token of the expected format). A failure after the start carries the `shape` of the
screen (counts and flags, `setup_token.shape`) for the runner log. The URL, the code, the token and the screen text are
never logged.
"""

from __future__ import annotations

import asyncio
import imaplib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runner.identity import ensure_private_dir
from agent_runner.login import capture, pty, setup_token
from agent_runner.login.pty import PtyProcess, SpawnFailed

URL_POLL = 0.1
PROMPT_SETTLE = 0.3
"""Seconds between the input field showing and typing: the program may enable its input just after drawing it."""
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


class LoginFailed(Exception):
    def __init__(self, code: str, shape: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.shape = shape
        """The form of the screen at the failure (`setup_token.shape`), None before the command ran."""


@dataclass(frozen=True)
class LoginOptions:
    """Time limits and seams of the login (tests replace them through `context.login_options`)."""

    url_wait: float = 30.0
    """Seconds until the command must have opened the authorize URL (section 11.2 step 2)."""
    prompt_wait: float = 30.0
    """Seconds (from typing the code on) until the input field must be on the screen."""
    token_wait: float = 60.0
    """Seconds after the code until the command must have ended or shown its error."""
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
        "COLUMNS": str(pty.COLUMNS),
        "NO_COLOR": "1",
    }


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
                raise LoginFailed("timeout", _shape(process))
            await process.wait_change(min(remaining, URL_POLL))

    async def finish(self, code: str, opts: LoginOptions) -> str:
        """Type the code once the input field is shown; the token."""
        process = self.process
        assert process is not None
        await _until(process, opts.prompt_wait, lambda p: _prompt_shown(p) or _error_shown(p))
        if not _prompt_shown(process):
            # ended, or an error before the code (the program gave up by itself): not the code's fault
            failed = "login_cli_failed" if process.eof or _error_shown(process) else "timeout"
            raise LoginFailed(failed, _shape(process))
        await asyncio.sleep(PROMPT_SETTLE)
        try:
            process.write(code + "\r")
        except OSError:
            raise LoginFailed("login_cli_failed", _shape(process)) from None
        if not await _until(process, opts.token_wait, _error_shown):
            raise LoginFailed("timeout", _shape(process))
        if not process.eof:
            raise LoginFailed("code_invalid", _shape(process))
        status = await process.reap()
        lines = process.screen.lines()
        if status != 0:
            failed = "code_invalid" if setup_token.shown(lines, setup_token.ERROR_LABEL) else "login_cli_failed"
            raise LoginFailed(failed, _shape(process))
        token = setup_token.parse_token(lines)
        if token is None:
            raise LoginFailed("token_invalid", _shape(process))
        return token

    def close(self) -> None:
        """Kill the command and remove the temporary directory (idempotent)."""
        if self.process is not None:
            self.process.kill()
            self.process = None
        shutil.rmtree(self.workdir, ignore_errors=True)
        if self.workdir.exists():
            self.left = True


def _shape(process: PtyProcess) -> str:
    return setup_token.shape(process.screen.lines())


def _prompt_shown(process: PtyProcess) -> bool:
    return setup_token.shown(process.screen.lines(), setup_token.PROMPT_LABEL)


def _error_shown(process: PtyProcess) -> bool:
    return setup_token.shown(process.screen.lines(), setup_token.ERROR_LABEL)


async def _until(process: PtyProcess, seconds: float, condition: Callable[[PtyProcess], bool]) -> bool:
    """Wait until `condition` holds or the command closed the terminal (True), False after `seconds`."""
    deadline = process.loop.time() + seconds
    while not condition(process) and not process.eof:
        remaining = deadline - process.loop.time()
        if remaining <= 0:
            return False
        await process.wait_change(min(remaining, URL_POLL))
    return True
