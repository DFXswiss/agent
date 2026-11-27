"""U-DBL-login: the login-CLI double (``claude setup-token`` mimic), in process and in a real PTY; its screens are
read through the runner's terminal screen (`agent_runner.login.screen`), as the runner reads them (KOR-21)."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import termios
import time
import urllib.error
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.login_cli import cli, codes
from agent_doubles.login_cli.cli import PROMPT, authorize_url, exchange, main, open_browser, read_line
from agent_runner.login.screen import Screen

from .conftest import Login, Terminal

LOCAL_REDIRECT = "http://localhost:45454/callback"


def url_rows(lines: list[str]) -> str:
    """The URL as shown: its first row and the rows it is wrapped onto, joined."""
    start = next(i for i, line in enumerate(lines) if line.startswith("https://"))
    end = lines.index("", start)
    return "".join(lines[start:end])


def localhost_url(page_url: str, seed: str) -> str:
    return authorize_url(page_url, seed, redirect_uri=LOCAL_REDIRECT)


TOKEN_LINE = " Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>"
MARKER = " Your OAuth token (valid for 1 year):"
ERROR = " ✘ OAuth error: "


def _argv(login: Login, *extra: str) -> list[str]:
    assert login.ca is not None
    return ["setup-token", "--auth-url", login.auth.page_url, "--ca-file", str(login.ca.ca_file), "--seed", "s", *extra]


# --- in a PTY, as the runner starts it -----------------------------------------------------------------------------


def test_setup_token_relay_in_a_pty(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    config = tmp_path / "claude-config"
    term = terminal(*_argv(login, "--prompt-delay", "0.5"), extra_env={"CLAUDE_CONFIG_DIR": str(config)})
    lines = term.read_until_shown(PROMPT)
    assert PROMPT not in term.text  # placed word by word, like the real program
    url = url_rows(lines)
    captured = (tmp_path / "url.txt").read_text()
    assert url == authorize_url(login.auth.page_url, "s")  # wrapped at 120 columns, joined
    assert captured == localhost_url(login.auth.page_url, "s")
    assert captured != url
    assert " Browser didn't open? Use the URL below to sign in (c to copy)" in lines
    assert lines[lines.index(" " + PROMPT) + 1].startswith(" ╭─")
    term.type(login.code_for(url))
    started = time.monotonic()
    assert term.finish() == 0
    assert time.monotonic() - started >= cli.SUCCESS_EXIT  # the token screen stays a moment, then the double ends
    token = codes.access_token(codes.DEFAULT_SEED, 1)
    lines = [line for line in term.lines if line]
    assert lines[-4:] == [
        MARKER,
        " " + token,
        " Store this token securely. You won't be able to see it again.",
        TOKEN_LINE,
    ]
    assert " ✓ Long-lived authentication token created" in lines
    assert not any("Finishing" in line or PROMPT in line or "https://" in line for line in lines)  # overwritten
    assert json.loads((config / ".claude.json").read_text()) == {"double": "login_cli"}
    assert login.auth.state()["milestones"][-1] == "token"


def test_a_wrong_code_shows_the_error_and_the_double_stays_open(
    login: Login, terminal: Callable[..., Terminal]
) -> None:
    term = terminal(*_argv(login, "--prompt-delay", "0", "--code-timeout", "30"))
    term.read_until_shown(PROMPT)
    term.type("not-the-code#" + codes.oauth_state("s"))
    lines = term.read_until_shown("Press Enter to retry")
    assert ERROR + "Invalid 'code' in request." in lines
    term.type("")  # Enter: the double ignores it
    time.sleep(0.5)
    assert term.process.poll() is None
    assert codes.TOKEN_PREFIX not in term.text


def test_a_code_typed_before_the_input_field_is_dropped(
    login: Login, terminal: Callable[..., Terminal], tmp_path: Path
) -> None:
    term = terminal(*_argv(login, "--prompt-delay", "2"))
    term.read_until_shown("Opening browser to sign in…")
    term.type("early-code#" + codes.oauth_state("s"))  # would be refused if it were read
    lines = term.read_until_shown(PROMPT)
    url = url_rows(lines)
    captured = (tmp_path / "url.txt").read_text()
    assert url == authorize_url(login.auth.page_url, "s")
    assert captured == localhost_url(login.auth.page_url, "s")
    assert captured != url
    term.type(login.code_for(url))
    assert term.finish() == 0
    assert codes.access_token(codes.DEFAULT_SEED, 1) in "".join(term.lines)


def test_a_narrow_window_wraps_the_token(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    term = terminal(*_argv(login, "--prompt-delay", "0"), columns=40)
    lines = term.read_until_shown(PROMPT)
    url = url_rows(lines)
    captured = (tmp_path / "url.txt").read_text()
    assert captured == localhost_url(login.auth.page_url, "s")
    assert captured != url
    term.type(login.code_for(url))
    assert term.finish() == 0
    lines = term.lines
    start = lines.index(" Your OAuth token (valid for 1 year):") + 2
    token = codes.access_token(codes.DEFAULT_SEED, 1)
    assert [line.strip() for line in lines[start : start + 3]] == [token[:39], token[39:78], token[78:]]
    assert lines[start + 3] == ""


def test_timeout_variant_ignores_the_code(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    term = terminal(*_argv(login, "--variant", "timeout", "--code-timeout", "1", "--prompt-delay", "0"))
    lines = term.read_until_shown(PROMPT)
    url = url_rows(lines)
    captured = (tmp_path / "url.txt").read_text()
    assert captured == localhost_url(login.auth.page_url, "s")
    assert captured != url
    term.type(login.code_for(url))
    assert term.finish() == 1
    assert ERROR + "timed out waiting for the authorization code" in term.lines
    assert "token" not in login.auth.state()["milestones"]


def test_no_url_variant(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    term = terminal(*_argv(login, "--variant", "no-url", "--code-timeout", "0.5"))
    assert term.finish() == 1
    assert "https://" not in term.text
    assert not (tmp_path / "url.txt").exists()
    assert ERROR + "timed out waiting for the browser" in term.lines


def test_without_browser_the_url_is_still_shown(login: Login, terminal: Callable[..., Terminal]) -> None:
    term = terminal(*_argv(login, "--code-timeout", "0.5", "--prompt-delay", "0"), browser=False)
    lines = term.read_until_shown(PROMPT)
    assert url_rows(lines) == authorize_url(login.auth.page_url, "s")
    assert term.finish() == 1
    assert ERROR + "timed out waiting for the authorization code" in term.lines


# --- in process ----------------------------------------------------------------------------------------------------


class _Out(io.StringIO):
    """Standard output of an in-process run: hands `stdin` to the double once the input field is drawn (what is
    typed before is dropped)."""

    def __init__(self, fd: int, stdin: bytes | None) -> None:
        super().__init__()
        self.fd, self.stdin = fd, stdin

    def write(self, text: str) -> int:
        written = super().write(text)
        if self.fd >= 0 and "╰" in text:
            if self.stdin is not None:
                os.write(self.fd, self.stdin)
                os.close(self.fd)
            self.fd = -1
        return written


def _run(argv: list[str], stdin: bytes | None = b"", **kwargs: Any) -> tuple[int, list[str]]:
    """Run the double in process; the exit code and its screen. `stdin` None: the input stays open and empty."""
    read_fd, write_fd = os.pipe()
    out = _Out(write_fd, stdin)
    try:
        code = main(
            [*argv, "--prompt-delay", "0"], stdin_fd=read_fd, out=out, environ=kwargs.pop("environ", {}), **kwargs
        )
    finally:
        os.close(read_fd)
        if out.fd >= 0 or stdin is None:
            os.close(write_fd)
    screen = Screen(50, cli.DEFAULT_COLUMNS)
    screen.feed(out.getvalue())
    return code, [line for line in screen.lines() if line]


def test_in_process_success_and_browser_call(login: Login) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> None:
        calls.append(argv)

    url = authorize_url(login.auth.page_url, "s")
    pasted = login.code_for(url)
    code, lines = _run(_argv(login), (pasted + "\n").encode(), environ={"BROWSER": "/capture"}, run=run)
    assert code == 0, lines
    assert calls == [["/capture", localhost_url(login.auth.page_url, "s")]]
    assert lines[-1] == TOKEN_LINE


@pytest.mark.parametrize(
    ("pasted", "message"),
    [
        (b"", "no code was entered"),
        (b"no-separator\n", "Invalid code"),
        (b"#state\r", "Invalid code"),
        (b"code#other-state\n", "Invalid state parameter"),
    ],
)
def test_in_process_input_errors(login: Login, pasted: bytes, message: str) -> None:
    code, lines = _run(_argv(login), pasted)
    assert code == 1
    assert lines[-2:] == [ERROR + message, " Press Enter to retry"]


def test_in_process_read_timeout(login: Login) -> None:
    code, lines = _run(_argv(login, "--code-timeout", "0.1"), None)
    assert (code, lines[-2]) == (1, ERROR + "timed out waiting for the authorization code")


def test_after_an_error_the_double_stays_until_the_code_timeout(login: Login) -> None:
    ticks = iter([0.0, 0.0, 1.0, 1.0, 5.0, 5.0, 31.0])
    code, lines = _run(_argv(login, "--code-timeout", "30"), b"x\nmore\n", clock=lambda: next(ticks, 99.0))
    assert code == 1
    assert lines[-2] == ERROR + "Invalid code"


def test_a_spinner_turns_while_the_browser_opens(login: Login) -> None:
    out = io.StringIO()
    ticks = iter([0.0, 0.1, 0.2, 0.3])
    naps: list[float] = []
    flow = cli.Flow(
        cli.build_parser().parse_args(_argv(login, "--prompt-delay", "0.25")),
        login.auth.page_url,
        None,
        cli.Painter(out, 80),
        {},
        lambda *a, **k: None,
        lambda *a, **k: None,
        lambda: next(ticks),
        naps.append,
        lambda *a: ([], [], []),
    )
    flow.spin("Opening browser to sign in…", 0.25)
    assert naps == [cli.SPIN] * 2
    assert out.getvalue().count("Opening") == 1  # the text is written once, then only the spinner cell changes
    assert [c for c in out.getvalue() if c in cli.SPINNER] == list(cli.SPINNER[:3])


def test_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run([])[0] == 2
    assert _run(["login", "--auth-url", "https://127.0.0.1:1"])[0] == 2
    for url in ("ftp://127.0.0.1:1", "https://127.0.0.1:1/path", "https://127.0.0.1:1?x=1", "https://"):
        assert _run(["--auth-url", url])[0] == 2
        assert "--auth-url must be a base URL" in capsys.readouterr().err
    assert _run(["--auth-url", "https://127.0.0.1:1", "--ca-file", str(tmp_path / "missing.pem")])[0] == 2
    assert main(["--help"]) == 0
    assert _run(["--auth-url", "https://127.0.0.1:1/"], b"x\n")[0] == 1


# --- drawing -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "drawn"),
    [
        ("", " Paste code here", "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere"),
        (" Paste code here", " Paste code here", ""),
        (" ✢ Opening", " ✳ Opening", "\x1b[2G✳"),
        (" · Finishing sign-in…", " ✓ Long-lived", "\x1b[2G✓\x1b[4GLo\x1b[7Gg-l\x1b[11Gved\x1b[K"),
        ("abc def", "abc", "\x1b[4G\x1b[K"),
        ("ab", "a b", "\x1b[2G b"),
        ("abc   ", "ab", "\x1b[3G\x1b[K"),
        ("abc   ", "abX", "\x1b[3GX"),
    ],
)
def test_rows_are_redrawn_cell_by_cell(old: str, new: str, drawn: str) -> None:
    assert cli.redraw(old, new) == drawn
    screen = Screen(1, 40)
    screen.feed(old + "\r" + drawn)
    assert screen.lines() == [new.rstrip()]


def test_a_shorter_frame_erases_the_rows_left_over() -> None:
    out = io.StringIO()
    painter = cli.Painter(out, 20)
    painter.static(["head"])
    painter.show(["one", "two", "three"])
    painter.show(["uno"])
    screen = Screen(10, 20)
    screen.feed(out.getvalue())
    assert screen.lines()[:5] == ["head", "uno", "", "", ""]
    painter.show(["a", "b"])
    screen = Screen(10, 20)
    screen.feed(out.getvalue())
    assert screen.lines()[:4] == ["head", "a", "b", ""]
    assert cli.wrap("", 4) == [""]


def test_the_width_comes_from_the_terminal(tmp_path: Path) -> None:
    assert cli.terminal_columns(io.StringIO()) == cli.DEFAULT_COLUMNS
    with (tmp_path / "file").open("w") as plain:
        assert cli.terminal_columns(plain) == cli.DEFAULT_COLUMNS


def test_raw_input_leaves_a_pipe_alone_and_restores_a_terminal() -> None:
    master, slave = os.openpty()
    try:
        before = termios.tcgetattr(slave)
        with cli.raw_input(slave):
            assert not termios.tcgetattr(slave)[3] & termios.ECHO
        modes = termios.ECHO | termios.ICANON
        assert termios.tcgetattr(slave)[3] & modes == before[3] & modes == modes  # restored (PENDIN may be set)
        with cli.raw_input(slave):
            os.close(master)
            master = -1
    finally:
        if master >= 0:
            os.close(master)
        os.close(slave)
    read_fd, write_fd = os.pipe()
    with cli.raw_input(read_fd):
        pass
    os.close(read_fd)
    os.close(write_fd)


def test_input_before_the_field_is_dropped_until_the_end_of_input() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"early\n")
        cli.drop_input(read_fd)
        os.write(write_fd, b"x")
        os.close(write_fd)
        cli.drop_input(read_fd)
        cli.drop_input(read_fd)
    finally:
        os.close(read_fd)


def test_browser_failures_are_ignored() -> None:
    def broken(argv: list[str], **kwargs: Any) -> None:
        raise OSError("no such file")

    def slow(argv: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 10)

    open_browser({"BROWSER": "/x"}, "https://u", broken)
    open_browser({"BROWSER": "/x"}, "https://u", slow)
    open_browser({"BROWSER": ""}, "https://u", broken)


def test_read_line_edge_cases() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"abc")
        calls: list[int] = []

        def wait(*args: Any) -> tuple[list[int], list[int], list[int]]:
            calls.append(1)
            if len(calls) == 2:
                os.write(write_fd, b"def\r\nrest")
            return [read_fd], [], []

        assert read_line(read_fd, 5, wait=wait) == "abcdef"
        assert len(calls) == 2
        ticks = iter([0.0, 0.0, 0.5, 2.0])
        assert read_line(read_fd, 1.0, clock=lambda: next(ticks), wait=lambda *a: ([], [], [])) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self) -> bytes:
        return self.body


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (urllib.error.HTTPError("u", 500, "x", Message(), io.BytesIO(b'{"error":"server_error"}')), "status code 500"),
        (urllib.error.HTTPError("u", 502, "x", Message(), io.BytesIO(b"<html>")), "status code 502"),
        (urllib.error.HTTPError("u", 400, "x", Message(), io.BytesIO(b"[1]")), "status code 400"),
        (urllib.error.URLError("refused"), "token request failed (URLError)"),
        (ConnectionResetError(), "token request failed (ConnectionResetError)"),
        (_Response(b"not json"), "token request failed (JSONDecodeError)"),
        (_Response(b"[]"), "no valid token"),
        (_Response(b'{"access_token": "short"}'), "no valid token"),
    ],
)
def test_exchange_failures(answer: Any, message: str) -> None:
    def urlopen(request: Any, **kwargs: Any) -> Any:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    with pytest.raises(cli.LoginError, match=re.escape(message)):
        exchange("https://127.0.0.1:1", "s", "code#" + codes.oauth_state("s"), None, urlopen)


def test_columns_override_the_width(login: Login) -> None:
    url = authorize_url(login.auth.page_url, "s")
    pasted = login.code_for(url)
    code, lines = _run(_argv(login, "--columns", "40"), (pasted + "\n").encode())
    assert code == 0, lines
    token = codes.access_token(codes.DEFAULT_SEED, 1)
    start = lines.index(MARKER) + 1
    assert [line.strip() for line in lines[start : start + 3]] == [token[:39], token[39:78], token[78:]]
    assert cli.wrap("abcdef", 0) == ["abcdef"]
    assert cli.wrap("abcdef", 4) == ["abcd", "ef"]
