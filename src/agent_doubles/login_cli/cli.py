"""The login-CLI double: behaves like ``claude setup-token`` against the auth-page double.

    python -m agent_doubles.login_cli setup-token --auth-url https://127.0.0.1:PORT --ca-file ca.pem [--seed S]

Like the real command it builds an OAuth authorize URL with PKCE (S256) and a state, starts ``$BROWSER <url>`` when
``BROWSER`` is set (the runner's capture helper), prints the URL, then prompts ``Paste code here if prompted > `` and
reads one line ``CODE#STATE`` from standard input (a PTY in the runner). It exchanges the code at the auth-page
double's token endpoint and prints the token block::

    ✓ Long-lived authentication token created

    Your OAuth token (valid for 1 year):

    <token>

    Store this token securely. You won't be able to see it again.

    Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>

Exit 0 after the token, 1 on ``OAuth error: …`` (a wrong code or state, an end of input, a refused exchange,
a timeout), 2 for invalid arguments. The state and verifier derive from ``--seed``, so a run is reproducible.
When ``CLAUDE_CONFIG_DIR`` is set, the double writes a small ``.claude.json`` there at start (as the real CLI creates
its configuration), so callers can check that a temporary configuration directory is cleaned up.

Variants (``--variant``): ``ok`` (default); ``no-url`` never opens a browser nor prints a URL and gives up after
``--code-timeout`` seconds; ``timeout`` prints the URL but ignores every input and fails after ``--code-timeout``.
``--columns N`` hard-wraps the URL and the token at N characters, as the real terminal UI does in a narrow terminal,
so a caller can prove that it does not depend on unwrapped output (the URL comes reliably through ``BROWSER``).
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
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlencode, urlsplit

from agent_doubles.login_cli import codes

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
CODE_TIMEOUT = 600.0
BROWSER_TIMEOUT = 10.0
PROMPT = "Paste code here if prompted > "
VARIANTS = ("ok", "no-url", "timeout")


class LoginError(Exception):
    """Ends the run with ``OAuth error: <message>``."""


def authorize_url(auth_url: str, seed: str) -> str:
    """The authorize URL of a run with `seed` against the auth-page double at `auth_url`."""
    _, challenge = codes.pkce(seed)
    query = {
        "code": "true",
        "client_id": codes.CLIENT_ID,
        "response_type": "code",
        "redirect_uri": auth_url + codes.CALLBACK_PATH,
        "scope": codes.SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": codes.oauth_state(seed),
    }
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
    """Start ``$BROWSER url`` like the real CLI; any failure is ignored (the URL is printed anyway)."""
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
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_doubles.login_cli", description="Login-CLI double (mimics claude setup-token)"
    )
    parser.add_argument("command", nargs="?", default="setup-token", choices=["setup-token"])
    parser.add_argument("--auth-url", required=True, help="base URL of the auth-page double")
    parser.add_argument("--ca-file", type=Path, help="CA certificate that signed the auth-page double")
    parser.add_argument("--seed", default=codes.DEFAULT_SEED, help="seed of the state and the PKCE verifier")
    parser.add_argument("--variant", choices=VARIANTS, default="ok")
    parser.add_argument("--code-timeout", type=float, default=CODE_TIMEOUT, help="seconds to wait for the code")
    parser.add_argument(
        "--columns", type=int, default=0, help="hard-wrap the URL and the token at this width (0: no wrapping)"
    )
    return parser


def _valid_auth_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") or parts.query:
        raise ValueError("--auth-url must be a base URL (scheme://host:port)")
    return value.rstrip("/")


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

    def say(text: str = "", end: str = "\n") -> None:
        stream.write(text + end)
        stream.flush()

    try:
        args = build_parser().parse_args(argv)
        auth_url = _valid_auth_url(args.auth_url)
        args.stdin_fd = stdin_fd
        context = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file is not None else None
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    except (ValueError, OSError, ssl.SSLError) as exc:
        print(f"login double: {exc}", file=sys.stderr)
        return EXIT_USAGE
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        (Path(config_dir) / ".claude.json").write_text(json.dumps({"double": "login_cli"}) + "\n", encoding="utf-8")
    say("Welcome to Claude Code (login double)")
    say()
    try:
        token = _login(
            args, auth_url, context, say, env=env, run=run, urlopen=urlopen, clock=clock, sleep=sleep, wait=wait
        )
    except LoginError as exc:
        say()
        say(f"OAuth error: {exc}")
        return EXIT_FAILED
    for line in TOKEN_BLOCK:
        if line == "{token}":
            for part in wrap(token, args.columns):
                say(part)
        else:
            say(line)
    return EXIT_OK


def wrap(text: str, columns: int) -> list[str]:
    """`text` cut into pieces of `columns` characters (a terminal UI hard-wraps long words); whole when 0."""
    if columns <= 0:
        return [text]
    return [text[i : i + columns] for i in range(0, len(text), columns)]


TOKEN_BLOCK = (
    "✓ Long-lived authentication token created",
    "",
    "Your OAuth token (valid for 1 year):",
    "",
    "{token}",
    "",
    "Store this token securely. You won't be able to see it again.",
    "",
    "Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>",
)


def _login(
    args: argparse.Namespace,
    auth_url: str,
    context: ssl.SSLContext | None,
    say: Callable[..., None],
    *,
    env: Mapping[str, str],
    run: Callable[..., Any],
    urlopen: Callable[..., Any],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    wait: Callable[..., Any],
) -> str:
    """The interactive part: URL, prompt, code, exchange; the token or LoginError."""
    if args.variant == "no-url":
        sleep(args.code_timeout)
        raise LoginError("timed out waiting for the browser")
    url = authorize_url(auth_url, args.seed)
    say("Opening browser to sign in…")
    open_browser(env, url, run)
    say("Browser didn't open? Use the URL below to sign in")
    say()
    for part in wrap(url, args.columns):
        say(part)
    say()
    say(PROMPT, end="")
    if args.variant == "timeout":
        sleep(args.code_timeout)
        raise LoginError("timed out waiting for the authorization code")
    try:
        pasted = read_line(args.stdin_fd, args.code_timeout, clock=clock, wait=wait)
    except EOFError:
        raise LoginError("no code was entered") from None
    if pasted is None:
        raise LoginError("timed out waiting for the authorization code")
    say()
    return exchange(auth_url, args.seed, pasted, context, urlopen)
