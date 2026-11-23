"""U-DBL-login: the login-CLI double (``claude setup-token`` mimic), in process and in a real PTY."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import urllib.error
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.login_cli import cli, codes
from agent_doubles.login_cli.cli import PROMPT, authorize_url, exchange, main, open_browser, read_line

from .conftest import Login, Terminal

TOKEN_LINE = "Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>"


def _argv(login: Login, *extra: str) -> list[str]:
    assert login.ca is not None
    return ["setup-token", "--auth-url", login.auth.page_url, "--ca-file", str(login.ca.ca_file), "--seed", "s", *extra]


# --- in a PTY, as the runner starts it -----------------------------------------------------------------------------


def test_setup_token_relay_in_a_pty(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    config = tmp_path / "claude-config"
    term = terminal(*_argv(login), extra_env={"CLAUDE_CONFIG_DIR": str(config)})
    term.read_until(PROMPT.encode())
    url = (tmp_path / "url.txt").read_text()
    assert url == authorize_url(login.auth.page_url, "s")
    assert url in term.text
    assert "Browser didn't open? Use the URL below to sign in" in term.text
    term.type(login.code_for(url))
    assert term.finish() == 0
    token = codes.access_token(codes.DEFAULT_SEED, 1)
    lines = term.text.splitlines()
    assert "✓ Long-lived authentication token created" in lines
    assert lines[lines.index("Your OAuth token (valid for 1 year):") + 2] == token
    assert lines[-1] == TOKEN_LINE
    assert json.loads((config / ".claude.json").read_text()) == {"double": "login_cli"}
    assert login.auth.state()["milestones"][-1] == "token"


def test_wrong_code_in_a_pty(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    term = terminal(*_argv(login))
    term.read_until(PROMPT.encode())
    term.type("not-the-code#" + codes.oauth_state("s"))
    assert term.finish() == 1
    assert term.text.rstrip().endswith("OAuth error: Invalid 'code' in request.")
    assert codes.TOKEN_PREFIX not in term.text


def test_timeout_variant_ignores_the_code(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    term = terminal(*_argv(login, "--variant", "timeout", "--code-timeout", "1"))
    term.read_until(PROMPT.encode())
    assert (tmp_path / "url.txt").is_file()
    term.type(login.code_for((tmp_path / "url.txt").read_text()))
    assert term.finish() == 1
    assert "OAuth error: timed out waiting for the authorization code" in term.text
    assert "token" not in login.auth.state()["milestones"]


def test_no_url_variant(login: Login, terminal: Callable[..., Terminal], tmp_path: Path) -> None:
    term = terminal(*_argv(login, "--variant", "no-url", "--code-timeout", "0.5"))
    assert term.finish() == 1
    assert "https://" not in term.text
    assert not (tmp_path / "url.txt").exists()
    assert "OAuth error: timed out waiting for the browser" in term.text


def test_without_browser_the_url_is_still_printed(login: Login, terminal: Callable[..., Terminal]) -> None:
    term = terminal(*_argv(login, "--code-timeout", "0.5"), browser=False)
    term.read_until(PROMPT.encode())
    assert authorize_url(login.auth.page_url, "s") in term.text
    assert term.finish() == 1
    assert "OAuth error: timed out waiting for the authorization code" in term.text


# --- in process ----------------------------------------------------------------------------------------------------


def _run(argv: list[str], stdin: bytes | None = b"", **kwargs: Any) -> tuple[int, str]:
    out = io.StringIO()
    read_fd, write_fd = os.pipe()
    if stdin is not None:
        os.write(write_fd, stdin)
        os.close(write_fd)
    try:
        code = main(argv, stdin_fd=read_fd, out=out, environ=kwargs.pop("environ", {}), **kwargs)
    finally:
        os.close(read_fd)
        if stdin is None:
            os.close(write_fd)
    return code, out.getvalue()


def test_in_process_success_and_browser_call(login: Login) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> None:
        calls.append(argv)

    url = authorize_url(login.auth.page_url, "s")
    pasted = login.code_for(url)
    code, text = _run(_argv(login), (pasted + "\n").encode(), environ={"BROWSER": "/capture"}, run=run)
    assert code == 0, text
    assert calls == [["/capture", url]]
    assert text.splitlines()[-1] == TOKEN_LINE


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
    code, text = _run(_argv(login), pasted)
    assert code == 1
    assert text.rstrip().endswith(f"OAuth error: {message}")


def test_in_process_read_timeout(login: Login) -> None:
    code, text = _run(_argv(login, "--code-timeout", "0.1"), None)
    assert (code, text.rstrip().splitlines()[-1]) == (1, "OAuth error: timed out waiting for the authorization code")


def test_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run([])[0] == 2
    assert _run(["login", "--auth-url", "https://127.0.0.1:1"])[0] == 2
    for url in ("ftp://127.0.0.1:1", "https://127.0.0.1:1/path", "https://127.0.0.1:1?x=1", "https://"):
        assert _run(["--auth-url", url])[0] == 2
        assert "--auth-url must be a base URL" in capsys.readouterr().err
    assert _run(["--auth-url", "https://127.0.0.1:1", "--ca-file", str(tmp_path / "missing.pem")])[0] == 2
    assert _run(["--help"])[0] == 0
    assert _run(["--auth-url", "https://127.0.0.1:1/"], b"x\n")[0] == 1


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


def test_columns_wrap_url_and_token(login: Login) -> None:
    url = authorize_url(login.auth.page_url, "s")
    pasted = login.code_for(url)
    code, text = _run(_argv(login, "--columns", "40"), (pasted + "\n").encode())
    assert code == 0, text
    lines = text.splitlines()
    assert url not in lines
    assert "".join(lines[lines.index(url[:40]) : lines.index(url[:40]) + -(-len(url) // 40)]) == url
    token = codes.access_token(codes.DEFAULT_SEED, 1)
    start = lines.index(token[:40])
    assert "".join(lines[start : start + 3]) == token
    assert cli.wrap("abcdef", 0) == ["abcdef"]
    assert cli.wrap("abcdef", 4) == ["abcd", "ef"]
