"""U-DBL-login (KOR-71): the full login of the login-CLI double (``auth login``) and the refresh grant of the
auth-page double - the link of the code page on the screen, the keychain asked through ``PATH`` (the file is written
only when it is refused), the store of the real program, and the rotation of refresh tokens."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_doubles.login_cli import cli, codes
from agent_doubles.login_cli.cli import PROMPT, VISIT, authorize_url, main
from agent_runner.login.screen import Screen

from .conftest import USER, Login
from .test_authpage import _exchange_body, _token_request


class _Out(io.StringIO):
    """Standard output of an in-process run: hands `stdin` to the double once the input field is drawn."""

    def __init__(self, fd: int, stdin: bytes | None) -> None:
        super().__init__()
        self.fd, self.stdin = fd, stdin

    def write(self, text: str) -> int:
        written = super().write(text)
        if self.fd >= 0 and ">\r\n" in text:
            if self.stdin is not None:
                os.write(self.fd, self.stdin)
                os.close(self.fd)
            self.fd = -1
        return written


def _argv(login: Login, *extra: str) -> list[str]:
    assert login.ca is not None
    return ["auth", "login", "--claudeai", "--auth-url", login.auth.page_url, "--ca-file", str(login.ca.ca_file),
            "--seed", "s", "--prompt-delay", "0", *extra]  # fmt: skip


def _run(
    argv: list[str], stdin: bytes | None, keychain: int | None, **environ: str
) -> tuple[int, list[str], list[Any]]:
    """Run the double in process; exit code, screen rows, the calls of `run` (browser and `security`)."""
    calls: list[Any] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs.get("env")))
        if argv[0] == "security" and keychain is None:
            raise OSError("no security")
        return subprocess.CompletedProcess(argv, keychain or 0)

    read_fd, write_fd = os.pipe()
    out = _Out(write_fd, stdin)
    try:
        environ = {"BROWSER": "/capture", "PATH": "/private", **environ}
        code = main(argv, stdin_fd=read_fd, out=out, environ=environ, run=run)
    finally:
        os.close(read_fd)
        if out.fd >= 0 or stdin is None:
            os.close(write_fd)
    screen = Screen(50, cli.DEFAULT_COLUMNS)
    screen.feed(out.getvalue())
    return code, [line for line in screen.lines() if line], calls


def test_the_full_login_shows_the_code_page_and_writes_the_store_when_the_keychain_is_refused(
    login: Login, tmp_path: Path
) -> None:
    url = authorize_url(login.auth.page_url, "s", scope=codes.FULL_SCOPE, email=USER)
    pasted = login.code_for(url)
    code, lines, calls = _run(
        _argv(login, "--email", USER), (pasted + "\r").encode(), 1, CLAUDE_CONFIG_DIR=str(tmp_path)
    )
    assert code == 0, lines
    assert "Opening browser to sign in…" in lines
    assert lines[lines.index(VISIT) + 1] == url
    assert lines[lines.index(VISIT) + 2] == f"{PROMPT} >"
    assert lines[-1] == "Login successful."
    browser, security = calls
    assert parse_qs(urlsplit(browser[0][1]).query)["redirect_uri"] == ["http://localhost:45454/callback"]
    assert security == (["security", *cli.KEYCHAIN_PROBE], {"PATH": "/private"})
    stored = json.loads((tmp_path / ".credentials.json").read_text())["claudeAiOauth"]
    assert stored["accessToken"] == codes.access_token(codes.DEFAULT_SEED, 1)
    assert stored["refreshToken"] == codes.refresh_token(codes.DEFAULT_SEED, 1)
    assert stored["scopes"] == codes.FULL_SCOPE.split()
    assert (stored["subscriptionType"], stored["rateLimitTier"]) == ("max", "default_claude_max_20x")
    assert (tmp_path / ".credentials.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / ".claude.json").read_text())["oauthAccount"] == {"emailAddress": USER}


@pytest.mark.parametrize("keychain", [0, 44])
def test_a_usable_keychain_gets_the_login_instead_of_the_file(login: Login, tmp_path: Path, keychain: int) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s", scope=codes.FULL_SCOPE))
    code, lines, _ = _run(_argv(login), (pasted + "\n").encode(), keychain, CLAUDE_CONFIG_DIR=str(tmp_path))
    assert (code, lines[-1]) == (0, "Login successful.")
    assert not (tmp_path / ".credentials.json").exists()


def test_a_missing_security_counts_as_refused_and_no_profile_asks_fewer_scopes(login: Login, tmp_path: Path) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s", scope=cli.NO_PROFILE_SCOPE))
    argv = _argv(login, "--variant", "no-profile")
    code, _, _ = _run(argv, (pasted + "\n").encode(), None, CLAUDE_CONFIG_DIR=str(tmp_path))
    assert code == 0
    stored = json.loads((tmp_path / ".credentials.json").read_text())["claudeAiOauth"]
    assert stored["scopes"] == cli.NO_PROFILE_SCOPE.split()


def test_without_a_configuration_directory_nothing_is_stored(login: Login) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s", scope=codes.FULL_SCOPE))
    for keychain in (1, 0):
        assert _run(_argv(login), (pasted + "\n").encode(), keychain)[0] == 0
        pasted = login.code_for(authorize_url(login.auth.page_url, "s", scope=codes.FULL_SCOPE))


@pytest.mark.parametrize(("stdin", "message"), [(b"wrong\n", "Invalid code"), (None, "timed out waiting")])
def test_a_failed_full_login_shows_the_error(login: Login, stdin: bytes | None, message: str) -> None:
    code, lines, _ = _run(_argv(login, "--code-timeout", "0.2"), stdin, 1)
    assert code == 1
    assert lines[-1].startswith(f"OAuth error: {message}")


def test_only_the_two_commands_are_known() -> None:
    assert main(["auth", "logout", "--auth-url", "https://127.0.0.1:1"]) == cli.EXIT_USAGE


def test_the_refresh_grant_rotates_the_refresh_token(login: Login) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s", scope=codes.FULL_SCOPE))
    status, answer = _token_request(login, _exchange_body(login, pasted))
    assert status == 200
    assert (answer["expires_in"], answer["scope"]) == (codes.FULL_LIFETIME, codes.FULL_SCOPE)
    first = answer["refresh_token"]
    refresh = {"grant_type": "refresh_token", "refresh_token": first, "client_id": codes.CLIENT_ID}
    status, renewed = _token_request(login, refresh)
    assert status == 200
    assert renewed["refresh_token"] != first
    assert renewed["access_token"] != answer["access_token"]
    assert renewed["scope"] == codes.FULL_SCOPE
    # the used refresh token is dead, as is an unknown one
    assert _token_request(login, refresh) == (
        400,
        {"error": "invalid_grant", "error_description": "Invalid refresh token"},
    )
    assert _token_request(login, {**refresh, "refresh_token": "unknown"})[0] == 400
    # malformed refresh requests are failures of the client
    assert _token_request(login, {**refresh, "client_id": "other"})[1]["error_description"] == "unknown client_id"
    assert (
        _token_request(login, {"grant_type": "refresh_token", "client_id": codes.CLIENT_ID})[1]["error_description"]
        == "missing refresh_token"
    )
    login.auth.run.failures.clear()


def test_a_setup_token_login_gets_no_refresh_token(login: Login) -> None:
    pasted = login.code_for(authorize_url(login.auth.page_url, "s"))
    status, answer = _token_request(login, _exchange_body(login, pasted))
    assert status == 200
    assert "refresh_token" not in answer
    assert answer["expires_in"] == codes.TOKEN_LIFETIME
