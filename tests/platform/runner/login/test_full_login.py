"""U-RUN-login (KOR-71, ADR-091 section 1): what the full login reads - the code page's link on the screen, the store
of the program (`.credentials.json`, the address in `.claude.json`) with its checks - and the refusing `security`
that keeps every login out of the keychain."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_runner.login import capture, full_login
from agent_runner.login.full_login import (
    FullLogin,
    LoginUnusable,
    expiry_text,
    read_login,
    scopes_of,
    screen_url,
    write_security,
)
from agent_runner.login.relay import login_env

URL = "https://claude.example/oauth/authorize?code=true&state=s"
ACCESS = "access-token-of-a-test"
REFRESH = "refresh-token-of-a-test"
EXPIRES = 1_795_000_000_000


def store(config: Path, oauth: Any = None, *, account: Any = None, raw: bytes | None = None) -> None:
    config.mkdir(exist_ok=True)
    data = {
        "accessToken": ACCESS,
        "refreshToken": REFRESH,
        "expiresAt": EXPIRES,
        "scopes": ["user:profile", "user:inference", "user:sessions:claude_code"],
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x",
    }
    if isinstance(oauth, dict):
        data.update(oauth)
    document = raw if raw is not None else json.dumps({"claudeAiOauth": data if oauth != "absent" else None}).encode()
    (config / full_login.STORE).write_bytes(document)
    if account is not None:
        (config / full_login.ACCOUNT_FILE).write_text(json.dumps(account))


def test_the_refusing_security_comes_first_and_refuses(tmp_path: Path) -> None:
    script = write_security(tmp_path)
    assert script.stat().st_mode & 0o777 == 0o700
    assert subprocess.run([str(script), "add-generic-password", "-s", "x"], check=False).returncode == 1
    env = login_env(tmp_path, "/opt/claude/bin/claude", tmp_path)
    assert env["PATH"].split(os.pathsep)[:2] == [str(tmp_path), "/opt/claude/bin"]
    assert login_env(tmp_path, "/opt/claude/bin/claude")["PATH"].split(os.pathsep)[0] == "/opt/claude/bin"
    with pytest.raises(FileExistsError):
        write_security(tmp_path)


@pytest.mark.parametrize(
    ("rows", "found"),
    [
        (["Opening browser to sign in…"], None),
        ([full_login.VISIT_LABEL, ""], None),
        ([full_login.VISIT_LABEL, "Paste code here if prompted >"], None),
        (["  If the browser didn't open, visit:  ", URL[:30], URL[30:], "", "Paste code here if prompted >"], URL),
        ([full_login.VISIT_LABEL, URL, "Paste code here if prompted >"], URL),
    ],
)
def test_the_link_is_read_from_the_rows_after_the_label(rows: list[str], found: str | None) -> None:
    assert screen_url(rows) == found


@pytest.mark.parametrize("url", ["ftp://x/y", "https://" + "a" * capture.MAX_URL, "https://exämple.test/x"])
def test_a_link_out_of_form_is_refused(url: str) -> None:
    with pytest.raises(capture.UrlInvalid):
        screen_url([full_login.VISIT_LABEL, url])


def test_the_login_is_read_with_plan_and_address(tmp_path: Path) -> None:
    store(tmp_path, account={"oauthAccount": {"emailAddress": "team@example.com"}})
    found = read_login(tmp_path)
    assert found == FullLogin(
        token=ACCESS,
        refresh_token=REFRESH,
        expires_at="2026-11-18T11:06:40.000Z",
        scopes=("user:inference", "user:profile", "user:sessions:claude_code"),
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
        email="team@example.com",
    )
    assert ACCESS not in repr(found)
    assert REFRESH not in repr(found)
    assert found.body() == {
        "token": ACCESS,
        "refresh_token": REFRESH,
        "expires_at": "2026-11-18T11:06:40.000Z",
        "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
        "email": "team@example.com",
        "kind": "oauth_login",
        "subscription_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
    }


@pytest.mark.parametrize(
    "account",
    [None, {"oauthAccount": None}, {"oauthAccount": {"emailAddress": "no-at"}}, {"oauthAccount": {"emailAddress": 7}}],
)
def test_an_unknown_address_and_plan_are_left_out(tmp_path: Path, account: Any) -> None:
    store(tmp_path, {"subscriptionType": None, "rateLimitTier": "with space"}, account=account)
    found = read_login(tmp_path)
    assert (found.email, found.subscription_type, found.rate_limit_tier) == (None, None, None)
    assert set(found.body()) == {"token", "refresh_token", "expires_at", "scopes", "email", "kind"}


def test_a_malformed_settings_file_gives_no_address(tmp_path: Path) -> None:
    store(tmp_path)
    (tmp_path / full_login.ACCOUNT_FILE).write_text("{not json")
    assert read_login(tmp_path).email is None
    os.remove(tmp_path / full_login.ACCOUNT_FILE)
    os.mkdir(tmp_path / full_login.ACCOUNT_FILE)  # not a regular file: never read
    assert read_login(tmp_path).email is None


@pytest.mark.parametrize(
    ("oauth", "raw", "code"),
    [
        ("absent", None, "token_invalid"),
        (None, b"{not json", "token_invalid"),
        (None, b"x" * (full_login.MAX_FILE + 1), "token_invalid"),
        ({"accessToken": ""}, None, "token_invalid"),
        ({"refreshToken": None}, None, "token_invalid"),
        ({"scopes": "user:profile"}, None, "token_invalid"),
        ({"scopes": ["User Profile"]}, None, "token_invalid"),
        ({"scopes": ["user:inference"] * 33}, None, "token_invalid"),
        ({"scopes": ["user:inference", "user:sessions:claude_code"]}, None, "scope_missing"),
        ({"scopes": ["user:profile"]}, None, "scope_missing"),
        ({"expiresAt": True}, None, "token_invalid"),
        ({"expiresAt": 0}, None, "token_invalid"),
        ({"expiresAt": "soon"}, None, "token_invalid"),
        ({"expiresAt": full_login.MAX_EXPIRY_MS + 1}, None, "token_invalid"),
    ],
)
def test_an_unusable_store_is_refused(tmp_path: Path, oauth: Any, raw: bytes | None, code: str) -> None:
    store(tmp_path, oauth, raw=raw)
    with pytest.raises(LoginUnusable) as refused:
        read_login(tmp_path)
    assert refused.value.code == code


def test_a_missing_or_linked_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LoginUnusable):
        read_login(tmp_path)  # the program kept the login elsewhere
    elsewhere = tmp_path / "elsewhere"
    store(elsewhere)
    (tmp_path / full_login.STORE).symlink_to(elsewhere / full_login.STORE)
    with pytest.raises(LoginUnusable):
        read_login(tmp_path)
    os.remove(tmp_path / full_login.STORE)
    os.mkdir(tmp_path / full_login.STORE)
    with pytest.raises(LoginUnusable):
        read_login(tmp_path)


def test_scopes_and_expiry_forms() -> None:
    assert scopes_of("user:profile user:inference user:profile") == ("user:inference", "user:profile")
    assert scopes_of([]) == ()
    assert expiry_text(1000) == "1970-01-01T00:00:01.000Z"
