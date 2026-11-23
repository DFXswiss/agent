"""`claude connect|sync|watch|service|setup-token` (11-agent-access.md sections 8.4 and 8.5).

The local Claude login is read from a `security` stand-in and from credential files; a canary refresh token in every
document must never reach any output, request, file or log. `setup-token` drives the login-CLI double in a PTY."""

from __future__ import annotations

import json
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_doubles.login_cli import codes
from agent_platform import claude
from agent_platform.account import Account
from agent_platform.errors import PlatformError
from agent_platform.state import Home
from agent_runner.crypto.credential import open_credential
from agent_runner.crypto.encoding import parse_json
from agent_runner.crypto.log.wraps import CRED, WrapSlot

from .conftest import Tool, pair_user
from .hubfake import HubFake, User

CANARY = "canary-refresh-" + "q" * 40
TOKEN = "claude-access-" + "a" * 40


def login(token: str = TOKEN, expires_ms: int | None = None, **extra: object) -> dict[str, Any]:
    expires = int(time.time() * 1000) + 3_600_000 if expires_ms is None else expires_ms
    oauth = {"accessToken": token, "refreshToken": CANARY, "expiresAt": expires, "scopes": ["user:inference"]}
    return {"claudeAiOauth": {**oauth, **extra}}


def stored_body(hub: HubFake, user: User, kind: str = claude.KIND) -> dict[str, Any]:
    record = hub.credentials[(user.id, kind)]["record"]
    epoch = record["header"]["epoch"]
    body = parse_json(open_credential(user.cred_keys[epoch], record, user.identity.sig_public()))
    assert isinstance(body, dict)
    return body


def no_canary_anywhere(tool: Tool, hub: HubFake, *outputs: object) -> None:
    assert all(CANARY.encode() not in body for body in hub.bodies)
    for path in tool.root.rglob("*"):
        if path.is_file() and "keychain" not in path.parts and path.name != ".credentials.json":
            assert CANARY not in path.read_text(errors="replace"), path
    assert CANARY not in json.dumps(outputs)


def test_connect_from_the_keychain(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login())
    code, document = tool.run("claude", "connect")
    assert (code, document["stored"], document["kind"], document["version"]) == (0, True, "login_token", 1)
    body = stored_body(hub, account)
    assert body["token"] == TOKEN
    assert body["kind"] == "login_token"
    assert body["email"] is None
    assert set(body) == {"token", "email", "kind", "expires_at"}
    assert TOKEN not in json.dumps(document)
    no_canary_anywhere(tool, hub, document)
    log = tool.security_log.read_text()
    assert "find-generic-password -s Claude Code-credentials" in log
    assert "add-generic-password -U -s Claude" not in log
    # status shows kind and expiry of the stored token
    status = tool.run("status")[1]
    row = next(item for item in status["credentials"] if item["kind"] == "claude_subscription")
    assert (row["token_kind"], row["expires_at"]) == ("login_token", body["expires_at"])


def test_connect_from_the_file_when_the_keychain_has_none(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_file(login())
    document = tool.run("claude", "connect")[1]
    assert document["stored"] is True
    no_canary_anywhere(tool, hub, document)


def test_file_on_linux(tool: Tool) -> None:
    tool.claude_file(login())
    assert claude.read_login(tool.env, platform="linux")[0] == TOKEN


def test_default_credentials_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude.credentials_file({}) == tmp_path / ".claude" / ".credentials.json"


def test_unreadable_keychain_program_falls_back(tool: Tool, tmp_path: Path) -> None:
    broken = tmp_path / "security"
    broken.write_text("not a program")
    broken.chmod(0o755)
    tool.claude_file(login())
    assert claude.read_login({**tool.env, "AGENT_PLATFORM_SECURITY": str(broken)}, platform="darwin")[0] == TOKEN


@pytest.mark.parametrize(
    "document",
    [
        "{not json " + CANARY,
        {"other": {}},
        {"claudeAiOauth": {"accessToken": 5, "refreshToken": CANARY, "expiresAt": 1}},
        {"claudeAiOauth": {"accessToken": "has space", "refreshToken": CANARY, "expiresAt": 1}},
        {"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": CANARY, "expiresAt": True}},
        {"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": CANARY, "expiresAt": "soon"}},
        {"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": CANARY}},
    ],
)
def test_parse_failures_report_no_content(tool: Tool, hub: HubFake, account: User, document: object) -> None:
    tool.claude_keychain(document) if not isinstance(document, str) else tool.claude_file(document)
    code, result = tool.run("claude", "connect")
    assert (code, result["error"]) == (1, "claude_login_missing")
    assert set(result) == {"ok", "error", "next"}
    no_canary_anywhere(tool, hub, result)


def test_missing_login(tool: Tool, account: User) -> None:
    assert tool.run("claude", "connect")[1]["error"] == "claude_login_missing"


def test_expired_login(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login(expires_ms=1000))
    result = tool.run("claude", "sync")[1]
    assert result["error"] == "claude_login_expired"
    assert "setup-token" in result["next"]
    no_canary_anywhere(tool, hub, result)


def test_sync_stores_only_changes(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login())
    assert tool.run("claude", "sync")[1]["stored"] is True
    assert tool.run("claude", "sync")[1] == {
        "ok": True,
        "stored": False,
        "reason": "unchanged",
        "kind": "login_token",
        "expires_at": stored_body(hub, account)["expires_at"],
    }
    tool.claude_keychain(login(token=TOKEN + "b"))
    document = tool.run("claude", "sync")[1]
    assert (document["stored"], document["version"]) == (True, 2)
    assert stored_body(hub, account)["token"] == TOKEN + "b"
    record = hub.credentials[(account.id, claude.KIND)]["record"]
    assert record["header"]["version"] == 2


def _setup_record(tool: Tool, hub: HubFake, account: User) -> None:
    opened = Account.open(Home.from_env(tool.env))
    try:
        claude.store(opened, {"token": "setup-" + "s" * 30, "email": None, "kind": "setup_token"}, replace=True)
    finally:
        opened.close()


def test_setup_token_record_is_kept_unless_replace(tool: Tool, hub: HubFake, account: User) -> None:
    _setup_record(tool, hub, account)
    tool.claude_keychain(login())
    for command in ("connect", "sync"):
        assert tool.run("claude", command)[1]["reason"] == "setup_token_kept"
    assert stored_body(hub, account)["kind"] == "setup_token"
    assert tool.run("claude", "sync", "--replace")[1]["stored"] is True
    assert stored_body(hub, account)["kind"] == "login_token"


def test_record_of_another_author_is_kept(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login())
    tool.run("claude", "connect")
    record = hub.credentials[(account.id, claude.KIND)]["record"]
    hub.credentials[(account.id, claude.KIND)]["record"] = {**record, "sig": record["sig"][::-1]}
    assert tool.run("claude", "sync")[1]["reason"] == "setup_token_kept"


def test_version_conflict_is_retried_once(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login())
    calls: list[int] = []

    def conflict(body: Any, request: Any) -> tuple[int, Any] | None:
        calls.append(1)
        return (409, {"error": "version_conflict"}) if len(calls) == 1 else None

    hub.hooks[("PUT", "/v1/me/credentials/claude_subscription")] = conflict
    assert tool.run("claude", "connect")[1]["stored"] is True
    assert len(calls) == 2
    hub.hooks[("PUT", "/v1/me/credentials/claude_subscription")] = lambda b, r: (409, {"error": "version_conflict"})
    result = tool.run("claude", "connect", "--replace")[1]
    assert (result["error"], result["hub_error"]) == ("hub_refused", "version_conflict")


def test_other_refusals_are_not_retried(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login())
    hub.hooks[("GET", "/v1/me/credentials/claude_subscription")] = lambda b, r: (403, {"error": "forbidden"})
    assert tool.run("claude", "connect")[1]["hub_error"] == "forbidden"
    hub.hooks[("GET", "/v1/me/credentials/claude_subscription")] = lambda b, r: (200, {"record": "x"})
    assert tool.run("claude", "connect")[1]["reason"] == "response_invalid"


def test_without_a_verified_credential_key(tool: Tool, hub: HubFake) -> None:
    user = hub.add_user(cred=False)
    other = hub.add_user()
    pair_user(tool, hub, user)
    # a wrap of the user's scope made by someone else is ignored (only self-wraps count, W1)
    hub.add_wrap(other, WrapSlot(CRED, user.id, 1, user.id), user.identity.kem_public(), b"k" * 32)
    # and one that does not open
    hub.wraps.append({"scope": CRED, "scope_id": user.id, "epoch": "1", "recipient_id": user.id, "wrapped_by": user.id})
    tool.claude_keychain(login())
    result = tool.run("claude", "connect")[1]
    assert (result["error"], result["reason"]) == ("hub_refused", "cred_key_missing")


# --- watch ---


def test_watch_syncs_in_a_loop(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(claude, "time", SimpleNamespace(time=time.time, sleep=slept.append))
    tool.claude_keychain(login())
    document = tool.run("claude", "watch", "--interval", "7", "--iterations", "3")[1]
    assert document == {"ok": True, "runs": 3, "stored": 1}
    assert slept == [7, 7]


def test_watch_reports_failures_and_goes_on(
    tool: Tool, account: User, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(claude, "time", SimpleNamespace(time=time.time, sleep=lambda seconds: None))
    document = tool.run("claude", "watch", "--iterations", "2")[1]
    assert document == {"ok": True, "runs": 2, "stored": 0}
    assert capsys.readouterr().err.count("claude sync failed: claude_login_missing") == 2


def test_watch_stops_when_revoked(tool: Tool, hub: HubFake, account: User) -> None:
    tool.claude_keychain(login())
    hub.revoke(next(iter(hub.agents)))
    assert tool.run("claude", "watch", "--iterations", "2")[1]["error"] == "agent_revoked"


@pytest.mark.parametrize("argv", [["--interval", "0"], ["--iterations", "0"]])
def test_watch_arguments(tool: Tool, argv: list[str]) -> None:
    assert tool.run("claude", "watch", *argv)[1]["error"] == "invalid_argument"


# --- service ---


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def load(argv: list[str]) -> bool:
        calls.append(argv)
        return True

    monkeypatch.setattr(claude, "run_loader", load)
    return calls


def test_service_install_and_remove(tool: Tool, account: User, loader: list[list[str]]) -> None:
    document = tool.run("claude", "service", "install", "--interval", "120")[1]
    path = Path(document["path"])
    assert document["loaded"] is True
    assert path.parent == tool.root / "services"
    text = path.read_text()
    if sys.platform == "darwin":
        assert "<string>watch</string><string>--interval</string><string>120</string>" in text
        assert loader[0][:2] == ["launchctl", "bootstrap"]
    assert tool.env["AGENT_PLATFORM_HOME"] in text
    removed = tool.run("claude", "service", "remove")[1]
    assert (removed["removed"], removed["unloaded"]) == (True, True)
    assert not path.exists()
    assert tool.run("claude", "service", "remove")[1]["removed"] is False
    assert len(loader) == 2


def test_service_without_loading(tool: Tool, account: User, loader: list[list[str]]) -> None:
    assert tool.run("claude", "service", "install", "--no-load")[1]["loaded"] is False
    assert tool.run("claude", "service", "remove", "--no-load")[1]["unloaded"] is False
    assert loader == []


def test_service_needs_a_pairing(tool: Tool, loader: list[list[str]]) -> None:
    assert tool.run("claude", "service", "install")[1]["error"] == "not_paired"


def test_systemd_unit_and_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    text = claude.service_text({"AGENT_PLATFORM_HOME": "/x y"}, 300, platform="linux")
    assert 'Environment="AGENT_PLATFORM_HOME=/x y"' in text
    assert "ExecStart=" in text
    assert '"watch"' in text
    assert claude.service_path({}, "linux") == tmp_path / ".config" / "systemd" / "user" / claude.SYSTEMD_UNIT
    assert claude.service_path({}, "darwin") == tmp_path / "Library" / "LaunchAgents" / f"{claude.LAUNCHD_LABEL}.plist"
    unit = Path("/u") / claude.SYSTEMD_UNIT
    assert claude.load_command(unit, True, "linux") == ["systemctl", "--user", "enable", "--now", claude.SYSTEMD_UNIT]
    assert claude.load_command(unit, False, "linux")[2] == "disable"
    assert claude.load_command(unit, False, "darwin")[1] == "bootout"
    assert "&amp;" in claude.service_text({"AGENT_PLATFORM_HOME": "a&b"}, 1, platform="darwin")


def test_run_loader(tmp_path: Path) -> None:
    assert claude.run_loader(["true"]) is True
    assert claude.run_loader([str(tmp_path / "missing")]) is False


# --- setup-token ---

WRAPPER = """#!{python}
import json, os, sys, threading, time
from agent_doubles.login_cli import cli, codes
variant = os.environ.get("DOUBLE_VARIANT", "ok")
if sys.argv[1:] != ["setup-token"]:
    sys.exit(2)
if variant == "exit":
    print("nothing to see")
    sys.exit(0)
read, write = os.pipe()
seed = "double"

def browser(argv, **kwargs):
    # the user clicks Authorize; the program's own localhost callback receives the code (no code is typed)
    code = codes.auth_code(seed, 1) if variant == "ok" else "wrong"
    def later():
        time.sleep(0.5)
        os.write(write, (code + "#" + codes.oauth_state(seed) + "\\r").encode())
    threading.Thread(target=later, daemon=True).start()

class Answer:
    def __init__(self, data):
        self.data = data
    def read(self, *args):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False

def urlopen(request, **kwargs):
    sent = json.loads(request.data)
    if sent["code"] != codes.auth_code(seed, 1):
        import io, urllib.error
        detail = io.BytesIO(b'{{"error_description": "Invalid code"}}')
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {{}}, detail)
    return Answer(json.dumps({{"access_token": codes.access_token(seed, 1)}}).encode())

argv = ["setup-token", "--auth-url", "https://auth.invalid", "--prompt-delay", "0.05", "--code-timeout", "3"]
sys.exit(cli.main(argv, stdin_fd=read, environ={{"BROWSER": "browser"}}, run=browser, urlopen=urlopen))
"""


@pytest.fixture
def claude_double(tool: Tool) -> Callable[[str], dict[str, str]]:
    program = tool.root / "bin" / "claude"
    program.write_text(WRAPPER.format(python=sys.executable))
    program.chmod(program.stat().st_mode | stat.S_IXUSR)

    def env(variant: str) -> dict[str, str]:
        return {"AGENT_PLATFORM_CLAUDE": str(program), "DOUBLE_VARIANT": variant}

    return env


def test_setup_token_with_the_login_double(
    tool: Tool, hub: HubFake, account: User, claude_double: Callable[[str], dict[str, str]]
) -> None:
    code, document = tool.run("claude", "setup-token", env=claude_double("ok"))
    assert (code, document["kind"], document["stored"]) == (0, "setup_token", True), document
    body = stored_body(hub, account)
    assert body == {"token": codes.access_token("double", 1), "email": None, "kind": "setup_token"}
    assert body["token"] not in json.dumps(document)
    # a later login token does not replace it
    tool.claude_keychain(login())
    assert tool.run("claude", "sync")[1]["reason"] == "setup_token_kept"


@pytest.mark.parametrize("variant", ["wrong", "exit"])
def test_setup_token_failures(
    tool: Tool, account: User, claude_double: Callable[[str], dict[str, str]], variant: str
) -> None:
    assert tool.run("claude", "setup-token", env=claude_double(variant))[1]["error"] == "setup_token_failed"


def test_setup_token_timeout(
    tool: Tool, account: User, claude_double: Callable[[str], dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude, "SETUP_TIMEOUT_S", 0.2)
    assert tool.run("claude", "setup-token", env=claude_double("ok"))[1]["error"] == "setup_token_failed"


def test_setup_token_without_the_program(tool: Tool, account: User, tmp_path: Path) -> None:
    assert tool.run("claude", "setup-token", env={"PATH": str(tmp_path)})[1]["error"] == "claude_cli_missing"
    garbage = tmp_path / "claude"
    garbage.write_bytes(b"\x00\x01binary")
    garbage.chmod(0o755)
    result = tool.run("claude", "setup-token", env={"AGENT_PLATFORM_CLAUDE": str(garbage)})[1]
    assert result["error"] == "claude_cli_missing"


def test_claude_program_from_path(tmp_path: Path) -> None:
    program = tmp_path / "claude"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    assert claude.claude_program({"PATH": str(tmp_path)}) == str(program)
    program.chmod(0o644)
    with pytest.raises(PlatformError):
        claude.claude_program({"AGENT_PLATFORM_CLAUDE": str(program)})
