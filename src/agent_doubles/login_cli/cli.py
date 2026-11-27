"""The login-CLI double: behaves like ``claude setup-token`` against the auth-page double.

    python -m agent_doubles.login_cli setup-token --auth-url https://127.0.0.1:PORT --ca-file ca.pem [--seed S]

Like the real command (Claude Code 2.1.330, recorded in KOR-21) it builds an OAuth authorize URL with PKCE (S256) and
a state. When ``BROWSER`` is set it starts ``$BROWSER`` with that URL and ``redirect_uri``
``http://localhost:45454/callback`` (the real program's link redirects to ``http://localhost:<port>/callback``) and
draws the code page's authorize URL on the screen (the default redirect, which is the one the code exchange uses).
The terminal UI is drawn the way the real program does (`Painter`): words are placed with cursor positioning
(``ESC[<n>G``) instead of spaces, a new frame moves the cursor up over the previous one and overwrites only the cells
that changed (a space only where another character stood, ``ESC[K``/``ESC[2K`` for what is left), a spinner cell
changes in place.

1. "· Opening browser to sign in…" with a spinner for ``--prompt-delay`` seconds (the real program shows its input
   field about 4 s after the URL); whatever is typed until then is dropped, as the real program drops it.
2. "Browser didn't open? Use the URL below to sign in (c to copy)", an empty row, the code page's URL, "Paste code
   here if prompted" and an input box; it reads one line ``CODE#STATE`` from standard input (a PTY in the runner).
3. "· Finishing sign-in…", the code exchange at the auth-page double's token endpoint (the code page's redirect, not
   the localhost one handed to ``BROWSER``), then either
   - the token screen ("✓ Long-lived authentication token created", "Your OAuth token (valid for 1 year):", the
     token on its own rows, "Store this token securely. …", "Use this token by setting: export
     CLAUDE_CODE_OAUTH_TOKEN=<token>"), and exit 0 by itself after ``SUCCESS_EXIT`` seconds, or
   - "✘ OAuth error: <message>" and "Press Enter to retry": like the real program the double does **not** end; it
     ignores further input and exits 1 only after ``--code-timeout`` seconds or when the terminal closes.

Exit 2 for invalid arguments. The state and verifier derive from ``--seed``, so a run is reproducible. When
``CLAUDE_CONFIG_DIR`` is set, the double writes a small ``.claude.json`` there at start (as the real CLI creates its
configuration), so callers can check that a temporary configuration directory is cleaned up.

``auth login [--claudeai] [--email ADDRESS]`` (KOR-71) behaves like the full login of the real program (2.1.330,
probed up to the code input): ``$BROWSER`` gets an authorize URL whose redirect is ``http://localhost:<port>/callback``;
the screen shows "Opening browser to sign in…", "If the browser didn't open, visit:", the code page's authorize URL
(scopes `codes.FULL_SCOPE`, ``login_hint`` with ``--email``) and "Paste code here if prompted >". After the code
exchange it asks the keychain whether it can be used - ``security find-generic-password -s "Claude
Code-credentials-double"`` through ``PATH``, a read only - and, when ``security`` answers 0 or 44 (the real tool:
found or not found, i.e. a usable keychain), stores nothing (the real program would write the keychain); only when the
keychain is refused does it write ``$CLAUDE_CONFIG_DIR/.credentials.json`` (``claudeAiOauth`` with ``accessToken``,
``refreshToken``, ``expiresAt`` in ms, ``scopes``, ``subscriptionType`` ``max``, ``rateLimitTier``) and
``oauthAccount.emailAddress`` into ``.claude.json``, shows "Login successful." and exits 0. Variant ``no-profile``
asks for the scopes without ``user:profile``.

Variants (``--variant``): ``ok`` (default); ``no-url`` never opens a browser nor shows a URL and gives up after
``--code-timeout`` seconds; ``timeout`` shows the URL and the input field but ignores every input and fails after
``--code-timeout``. The layout follows the terminal's width (``--columns`` overrides it): longer rows, the URL and the
token are hard-wrapped, as the real UI does in a narrow window.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import ssl
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlencode, urlsplit

from agent_doubles.login_cli import codes

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
CODE_TIMEOUT = 600.0
BROWSER_TIMEOUT = 10.0
PROMPT_DELAY = 1.5
SUCCESS_EXIT = 0.5
"""Seconds the token screen stays before the command ends by itself (the real program: 500 ms)."""
SPIN = 0.12
SPINNER = "·✢✳✶✻✽"
DEFAULT_COLUMNS = 1000
"""The width when standard output is no terminal (in-process tests)."""
PROMPT = "Paste code here if prompted"
ERROR_HINT = "Press Enter to retry"
TOKEN_MARKER = "Your OAuth token (valid for 1 year):"
VARIANTS = ("ok", "no-url", "timeout", "no-profile")
VISIT = "If the browser didn't open, visit:"
KEYCHAIN_PROBE = ("find-generic-password", "-s", "Claude Code-credentials-double")
KEYCHAIN_USABLE = (0, 44)
"""Exit codes of the real `security find-generic-password`: found, not found - both mean a usable keychain."""
NO_PROFILE_SCOPE = "user:inference user:sessions:claude_code"


class LoginError(Exception):
    """Ends the run with ``OAuth error: <message>``."""


def authorize_url(
    auth_url: str, seed: str, *, scope: str = codes.SCOPE, redirect_uri: str | None = None, email: str | None = None
) -> str:
    """The authorize URL of a run with `seed` against the auth-page double at `auth_url`."""
    _, challenge = codes.pkce(seed)
    query = {
        "code": "true",
        "client_id": codes.CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri or auth_url + codes.CALLBACK_PATH,
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": codes.oauth_state(seed),
    }
    if email is not None:
        query["login_hint"] = email
    return f"{auth_url}{codes.AUTHORIZE_PATH}?{urlencode(query)}"


def read_line(
    fd: int,
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[..., Any] = select.select,
) -> str | None:
    """One line from `fd` (CR or LF ends it), None after `timeout` seconds; EOFError at end of input."""
    deadline = clock() + timeout
    buffer = b""
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        ready, _, _ = wait([fd], [], [], remaining)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            raise EOFError
        buffer += chunk
        for end in (b"\r", b"\n"):
            if end in buffer:
                return buffer.split(end, 1)[0].decode("utf-8", "replace").strip()


def open_browser(environ: Mapping[str, str], url: str, run: Callable[..., Any]) -> None:
    """Start ``$BROWSER url`` like the real CLI; any failure is ignored (the program draws its code page link on the
    screen anyway)."""
    browser = environ.get("BROWSER")
    if not browser:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        run(
            [browser, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=BROWSER_TIMEOUT,
            check=False,
        )


def exchange(
    auth_url: str,
    seed: str,
    pasted: str,
    context: ssl.SSLContext | None,
    urlopen: Callable[..., Any],
) -> str:
    """The token for the pasted ``CODE#STATE``; LoginError otherwise."""
    return str(exchange_answer(auth_url, seed, pasted, context, urlopen)["access_token"])


def exchange_answer(
    auth_url: str,
    seed: str,
    pasted: str,
    context: ssl.SSLContext | None,
    urlopen: Callable[..., Any],
) -> dict[str, Any]:
    """The token endpoint's answer for the pasted ``CODE#STATE``; LoginError otherwise."""
    code, separator, state = pasted.partition("#")
    if not code or not separator:
        raise LoginError("Invalid code")
    if state != codes.oauth_state(seed):
        raise LoginError("Invalid state parameter")
    verifier, _ = codes.pkce(seed)
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "client_id": codes.CLIENT_ID,
        "redirect_uri": auth_url + codes.CALLBACK_PATH,
        "code_verifier": verifier,
        "expires_in": codes.TOKEN_LIFETIME,
    }
    request = urllib.request.Request(
        auth_url + codes.TOKEN_PATH,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30, context=context) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error_description")
        except (ValueError, AttributeError):
            detail = None
        raise LoginError(detail or f"Request failed with status code {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LoginError(f"token request failed ({type(exc).__name__})") from None
    token = answer.get("access_token") if isinstance(answer, dict) else None
    if not isinstance(token, str) or not codes.TOKEN_PATTERN.fullmatch(token):
        raise LoginError("the token response holds no valid token")
    return dict(answer)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_doubles.login_cli", description="Login-CLI double (mimics claude setup-token)"
    )
    parser.add_argument("command", nargs="*", default=["setup-token"], help="setup-token | auth login")
    parser.add_argument("--claudeai", action="store_true", help="auth login: the Claude subscription (the default)")
    parser.add_argument("--email", help="auth login: the address filled in on the login page")
    parser.add_argument("--auth-url", required=True, help="base URL of the auth-page double")
    parser.add_argument("--ca-file", type=Path, help="CA certificate that signed the auth-page double")
    parser.add_argument("--seed", default=codes.DEFAULT_SEED, help="seed of the state and the PKCE verifier")
    parser.add_argument("--variant", choices=VARIANTS, default="ok")
    parser.add_argument("--code-timeout", type=float, default=CODE_TIMEOUT, help="seconds to wait for the code")
    parser.add_argument(
        "--prompt-delay", type=float, default=PROMPT_DELAY, help="seconds from the URL until the input field shows"
    )
    parser.add_argument("--columns", type=int, default=0, help="lay out for this width (0: the terminal's width)")
    return parser


def _valid_auth_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query:
        raise ValueError("--auth-url must be a base URL (scheme://host:port)")
    return value.rstrip("/")


def terminal_columns(stream: TextIO) -> int:
    """The width of the terminal behind `stream`, `DEFAULT_COLUMNS` when it is none or reports no width."""
    try:
        return os.get_terminal_size(stream.fileno()).columns or DEFAULT_COLUMNS
    except (OSError, ValueError, AttributeError):
        return DEFAULT_COLUMNS


def wrap(text: str, columns: int) -> list[str]:
    """`text` cut into pieces of `columns` characters (a terminal UI hard-wraps long words); whole when 0."""
    if columns <= 0:
        return [text]
    return [text[i : i + columns] for i in range(0, len(text), columns)] or [""]


def _same(old: str, new: str, i: int) -> bool:
    """Cell `i` needs no write: it holds the same character, or a blank stays blank."""
    return old[i] == new[i] if i < len(old) else new[i] == " "


def redraw(old: str, new: str) -> str:
    """The output that turns the row `old` into `new` from column 1 on, as the real program draws: runs of changed
    cells, each placed with ``ESC[<n>G``; a space only where another character stood; ``ESC[K`` for a tail left."""
    out: list[str] = []
    col, i = 0, 0
    while i < len(new):
        if _same(old, new, i):
            i += 1
            continue
        j = i
        while j < len(new) and not _same(old, new, j):
            j += 1
        if col != i:
            out.append(f"\x1b[{i + 1}G")
        out.append(new[i:j])
        col = i = j
    if old[len(new) :].strip():
        if col != len(new):
            out.append(f"\x1b[{len(new) + 1}G")
        out.append("\x1b[K")
    return "".join(out)


class Painter:
    """Draws rows like the real program: static rows once, then frames that replace each other in place."""

    def __init__(self, stream: TextIO, columns: int) -> None:
        self.stream = stream
        self.columns = columns
        self.frame: list[str] = []

    def fit(self, rows: Sequence[str]) -> list[str]:
        return [piece.rstrip() for row in rows for piece in wrap(row, self.columns)]

    def emit(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def static(self, rows: Sequence[str]) -> None:
        self.emit("".join(redraw("", row) + "\r\n" for row in self.fit(rows)))

    def show(self, rows: Sequence[str]) -> None:
        """Replace the current frame: up over it, rewrite the changed cells row by row, erase the rows left over."""
        new, old = self.fit(rows), self.frame
        out = [f"\r\x1b[{len(old)}A"] if old else []
        out += [redraw(old[i] if i < len(old) else "", row) + "\r\n" for i, row in enumerate(new)]
        if len(old) > len(new):
            out.append("\x1b[J")  # the rows left over below the new frame
        self.emit("".join(out))
        self.frame = new


@contextlib.contextmanager
def raw_input(fd: int) -> Iterator[None]:
    """Read keys as the real program does (Node's raw mode): no echo, no line editing, CR stays CR; output processing
    (LF -> CR LF) stays on. Without this, the terminal would echo the code into the drawn screen. No terminal: as is."""
    if not os.isatty(fd):
        yield
        return
    saved = termios.tcgetattr(fd)
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(termios.ICRNL | termios.IXON | termios.BRKINT | termios.INPCK | termios.ISTRIP)
    attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    try:
        yield
    finally:
        with contextlib.suppress(termios.error):
            termios.tcsetattr(fd, termios.TCSANOW, saved)


def drop_input(fd: int, wait: Callable[..., Any] = select.select) -> None:
    """Read and drop what was typed so far (the real program has no input field yet); stops at end of input."""
    while wait([fd], [], [], 0)[0]:
        if not os.read(fd, 4096):
            return


def linger(fd: int, seconds: float, *, clock: Callable[[], float], wait: Callable[..., Any]) -> None:
    """Stay open after an error like the real program: drop input until `seconds` pass or the input ends."""
    deadline = clock() + seconds
    with contextlib.suppress(EOFError):
        while clock() < deadline:
            read_line(fd, deadline - clock(), clock=clock, wait=wait)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin_fd: int = 0,
    out: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = subprocess.run,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wait: Callable[..., Any] = select.select,
) -> int:
    """Run the double; the exit code."""
    stream = out if out is not None else sys.stdout
    env = os.environ if environ is None else environ
    try:
        args = build_parser().parse_args(argv)
        if args.command not in (["setup-token"], ["auth", "login"]):
            raise ValueError("command must be setup-token or auth login")
        auth_url = _valid_auth_url(args.auth_url)
        context = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file is not None else None
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    except (ValueError, OSError, ssl.SSLError) as exc:
        print(f"login double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    args.stdin_fd = stdin_fd
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        (Path(config_dir) / ".claude.json").write_text(json.dumps({"double": "login_cli"}) + "\n", encoding="utf-8")
    painter = Painter(stream, args.columns or terminal_columns(stream))
    painter.emit("\x1b[?25l\x1b[?2004h\x1b]0;Claude Code (login double)\x07")
    painter.static(["Welcome to Claude Code (login double)", "", HEADER, ""])
    flow = Flow(args, auth_url, context, painter, env, run, urlopen, clock, sleep, wait)
    if args.command == ["auth", "login"]:
        return flow.full_login(Path(config_dir) if config_dir else None)
    with raw_input(stdin_fd):
        try:
            token = flow.login()
        except LoginError as exc:
            painter.show([f" ✘ OAuth error: {exc}", "", f" {ERROR_HINT}"])
            linger(stdin_fd, args.code_timeout, clock=clock, wait=wait)
            painter.emit("\x1b[?25h")
            return EXIT_FAILED
    rows = [" ✓ Long-lived authentication token created", "", f" {TOKEN_MARKER}", ""]
    rows += [" " + part for part in wrap(token, max(painter.columns - 1, 1))]
    rows += ["", " Store this token securely. You won't be able to see it again.", ""]
    rows += [" Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>"]
    painter.show(rows)
    sleep(SUCCESS_EXIT)
    painter.emit("\x1b[?25h")
    return EXIT_OK


HEADER = " This will guide you through long-lived (1-year) auth token setup for your Claude account."


@dataclass
class Flow:
    """The interactive part: URL, input field, code, exchange; the token or LoginError."""

    args: argparse.Namespace
    auth_url: str
    context: ssl.SSLContext | None
    painter: Painter
    env: Mapping[str, str]
    run: Callable[..., Any]
    urlopen: Callable[..., Any]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    wait: Callable[..., Any]

    def spin(self, text: str, seconds: float) -> None:
        """A status row whose first cell turns for `seconds` (at least one frame)."""
        end = self.clock() + seconds
        step = 0
        while True:
            self.painter.show([f" {SPINNER[step % len(SPINNER)]} {text}"])
            step += 1
            if self.clock() >= end:
                return
            self.sleep(SPIN)

    def login(self) -> str:
        args = self.args
        if args.variant == "no-url":
            self.spin("Opening browser to sign in…", 0)
            self.sleep(args.code_timeout)
            raise LoginError("timed out waiting for the browser")
        local = authorize_url(self.auth_url, args.seed, redirect_uri="http://localhost:45454/callback")
        url = authorize_url(self.auth_url, args.seed)
        self.spin("Opening browser to sign in…", 0)
        open_browser(self.env, local, self.run)
        self.spin("Opening browser to sign in…", args.prompt_delay)
        drop_input(args.stdin_fd, self.wait)
        width = max(self.painter.columns - 4, 1)
        rows = [" Browser didn't open? Use the URL below to sign in (c to copy)", "", *wrap(url, self.painter.columns)]
        rows += ["", f" {PROMPT}", " ╭" + "─" * width + "╮", " │" + " " * width + "│", " ╰" + "─" * width + "╯"]
        self.painter.show(rows)
        if args.variant == "timeout":
            self.sleep(args.code_timeout)
            raise LoginError("timed out waiting for the authorization code")
        try:
            pasted = read_line(args.stdin_fd, args.code_timeout, clock=self.clock, wait=self.wait)
        except EOFError:
            raise LoginError("no code was entered") from None
        if pasted is None:
            raise LoginError("timed out waiting for the authorization code")
        self.spin("Finishing sign-in…", SPIN)
        return exchange(self.auth_url, args.seed, pasted, self.context, self.urlopen)

    def full_login(self, config_dir: Path | None) -> int:
        """``auth login`` (module docstring); the exit code."""
        args = self.args
        scope = NO_PROFILE_SCOPE if args.variant == "no-profile" else codes.FULL_SCOPE
        local = authorize_url(
            self.auth_url, args.seed, scope=scope, redirect_uri="http://localhost:45454/callback", email=args.email
        )
        url = authorize_url(self.auth_url, args.seed, scope=scope, email=args.email)
        with raw_input(args.stdin_fd):
            self.painter.static(["Opening browser to sign in…"])
            open_browser(self.env, local, self.run)
            self.sleep(args.prompt_delay)
            drop_input(args.stdin_fd, self.wait)
            self.painter.static([VISIT, *wrap(url, self.painter.columns), f"{PROMPT} >"])
            try:
                pasted = read_line(args.stdin_fd, args.code_timeout, clock=self.clock, wait=self.wait)
                if pasted is None:
                    raise LoginError("timed out waiting for the authorization code")
                answer = exchange_answer(self.auth_url, args.seed, pasted, self.context, self.urlopen)
            except (LoginError, EOFError) as exc:
                self.painter.static([f"OAuth error: {exc}"])
                linger(args.stdin_fd, args.code_timeout, clock=self.clock, wait=self.wait)
                return EXIT_FAILED
        if not self.keychain_refused() and config_dir is not None:
            self.painter.static(["Login successful."])  # the real program would have written the keychain
            return EXIT_OK
        if config_dir is not None:
            self.store(config_dir, answer)
        self.painter.static(["Login successful."])
        return EXIT_OK

    def keychain_refused(self) -> bool:
        """Ask ``security`` in ``PATH`` (a read only) whether the keychain can be used."""
        try:
            done = self.run(
                ["security", *KEYCHAIN_PROBE],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=BROWSER_TIMEOUT,
                check=False,
                env={"PATH": self.env.get("PATH", "")},
            )
        except (OSError, subprocess.SubprocessError):
            return True
        return done.returncode not in KEYCHAIN_USABLE

    def store(self, config_dir: Path, answer: Mapping[str, Any]) -> None:
        """The plaintext store of the real program and the account's address."""
        expires = int((time.time() + int(answer["expires_in"])) * 1000)
        oauth = {
            "accessToken": answer["access_token"],
            "refreshToken": answer.get("refresh_token"),
            "expiresAt": expires,
            "scopes": str(answer.get("scope", "")).split(),
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        }
        target = config_dir / ".credentials.json"
        target.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
        target.chmod(0o600)
        account = answer.get("account")
        email = account.get("email_address") if isinstance(account, Mapping) else None
        settings = {"double": "login_cli", "oauthAccount": {"emailAddress": email}}
        (config_dir / ".claude.json").write_text(json.dumps(settings) + "\n", encoding="utf-8")
