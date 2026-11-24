"""The Claude subscription (11-agent-access.md sections 8.4 and 8.5, ADR-056 section 7).

Reading the local Claude Code login is read-only: never a login, logout or refresh, never a write to the profile or
the keychain. macOS: the generic password `Claude Code-credentials` through `security find-generic-password -s ... -w`
(the program of `AGENT_PLATFORM_SECURITY`), then the credentials file; elsewhere the credentials file
(`$CLAUDE_CONFIG_DIR/.credentials.json`, else `~/.claude/.credentials.json`). Of the JSON `claudeAiOauth` only
`accessToken` and `expiresAt` (ms since the epoch) are taken; the parsed document, refresh token included, is dropped
at once and never stored, sent, logged or printed. A parse failure is `claude_login_missing` without any content.

Storing: a `claude_subscription` record (04-crypto.md section 4.4) with the body
`{"token", "email": null, "kind": "login_token"|"setup_token", "expires_at"?}`, encrypted under the user's newest
verified `CK` and signed by the identity as author, `PUT /v1/me/credentials/claude_subscription` with
`expected_version`. A `login_token` never replaces a `setup_token` record (nor a record the tool cannot read) unless
`replace`; a `409 version_conflict` is retried once on the re-read record.

`claude setup-token` runs the local program in a pseudo-terminal (`agent_runner.login.pty`); the program opens the
browser itself and finishes through its own localhost callback, so the user only approves. The token is read from the
program's screen (`agent_runner.login.setup_token.parse_token`) and never printed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_platform.account import Account
from agent_platform.credentials import Guard, put_record
from agent_platform.errors import PlatformError
from agent_platform.state import Home, keychain_available, run_security, security_program
from agent_runner.crypto.encoding import format_time
from agent_runner.login import setup_token as screen_labels
from agent_runner.login.pty import PtyProcess, SpawnFailed

KEYCHAIN_ITEM = "Claude Code-credentials"
KIND = "claude_subscription"
LOGIN_TOKEN = "login_token"
SETUP_TOKEN = "setup_token"
_TOKEN_RE = re.compile(r"[!-~]{1,4096}")
SETUP_TIMEOUT_S = 600.0
WATCH_INTERVAL_S = 300


def credentials_file(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CLAUDE_CONFIG_DIR", "")
    base = Path(configured) if configured else Path.home() / ".claude"
    return base / ".credentials.json"


def parse_login(text: str) -> tuple[str, int]:
    """`(accessToken, expiresAt)` of a Claude Code credentials document; `claude_login_missing` otherwise."""
    try:
        document: Any = json.loads(text)
        oauth = document["claudeAiOauth"]
        token, expires = oauth["accessToken"], oauth["expiresAt"]
    except Exception:
        raise PlatformError("claude_login_missing") from None
    finally:
        document = oauth = None
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise PlatformError("claude_login_missing")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        raise PlatformError("claude_login_missing")
    return token, expires


def read_login(environ: Mapping[str, str], platform: str = sys.platform) -> tuple[str, int]:
    """The local login: the keychain item on macOS, then the credentials file."""
    program = security_program(environ)
    if keychain_available(program, platform):
        try:
            done = run_security(program, ["find-generic-password", "-s", KEYCHAIN_ITEM, "-w"])
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0:
            return parse_login(done.stdout)
    try:
        text = credentials_file(environ).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PlatformError("claude_login_missing") from None
    return parse_login(text)


def current_login(environ: Mapping[str, str], now_ms: int) -> tuple[str, int]:
    token, expires = read_login(environ)
    if expires <= now_ms:
        raise PlatformError("claude_login_expired")
    return token, expires


def expiry_text(expires_ms: int) -> str:
    return format_time(datetime.fromtimestamp(expires_ms / 1000, UTC))


def _guard(replace: bool, only_changed: bool) -> Guard:
    """The rules of `claude connect|sync` for an existing record (module docstring)."""

    def decide(body: Mapping[str, Any], record: object, current: Mapping[str, Any] | None) -> dict[str, Any] | None:
        readable_login = current is not None and current.get("kind") == LOGIN_TOKEN
        if record is not None and not replace and body["kind"] == LOGIN_TOKEN and not readable_login:
            return {"stored": False, "reason": "setup_token_kept"}
        if only_changed and current is not None and current.get("token") == body["token"]:
            return {"stored": False, "reason": "unchanged"}
        return None

    return decide


def store(account: Account, body: dict[str, Any], *, replace: bool, only_changed: bool = False) -> dict[str, Any]:
    return put_record(account, KIND, body, _guard(replace, only_changed))


def login_body(token: str, expires_ms: int) -> dict[str, Any]:
    return {"token": token, "email": None, "kind": LOGIN_TOKEN, "expires_at": expiry_text(expires_ms)}


def connect(account: Account, environ: Mapping[str, str], *, replace: bool, only_changed: bool) -> dict[str, Any]:
    token, expires = current_login(environ, int(time.time() * 1000))
    result = store(account, login_body(token, expires), replace=replace, only_changed=only_changed)
    return {**result, "kind": LOGIN_TOKEN, "expires_at": expiry_text(expires)}


def watch(
    open_account: Callable[[], Account],
    environ: Mapping[str, str],
    *,
    interval: float,
    iterations: int | None,
    upkeep: Callable[[Account], object],
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """`sync` every `interval` seconds, each round after `upkeep` (the browser's jobs, `upkeep.sync`); failures other
    than a lost pairing are reported on stderr (codes only)."""
    runs = stored = 0
    while iterations is None or runs < iterations:
        if runs:
            (sleep or time.sleep)(interval)
        runs += 1
        try:
            account = open_account()
            try:
                upkeep(account)
                result = connect(account, environ, replace=False, only_changed=True)
            finally:
                account.close()
            stored += 1 if result["stored"] else 0
        except PlatformError as error:
            if error.code in ("agent_revoked", "not_paired", "state_damaged"):
                raise
            print(f"agent-platform: claude sync failed: {error.code}", file=sys.stderr)
    return {"runs": runs, "stored": stored}


# --- background service ---

LAUNCHD_LABEL = "swiss.dfx.agent-platform.claude-sync"
SYSTEMD_UNIT = "agent-platform-claude-sync.service"
PASSED_ENV = ("AGENT_PLATFORM_HOME", "AGENT_PLATFORM_SECURITY", "CLAUDE_CONFIG_DIR")


def service_path(environ: Mapping[str, str], platform: str = sys.platform) -> Path:
    """The user-level service file (`AGENT_PLATFORM_SERVICE_DIR` overrides the directory)."""
    configured = environ.get("AGENT_PLATFORM_SERVICE_DIR", "")
    if platform == "darwin":
        base = Path(configured) if configured else Path.home() / "Library" / "LaunchAgents"
        return base / f"{LAUNCHD_LABEL}.plist"
    base = Path(configured) if configured else Path.home() / ".config" / "systemd" / "user"
    return base / SYSTEMD_UNIT


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def service_text(environ: Mapping[str, str], interval: int, platform: str = sys.platform) -> str:
    argv = [sys.executable, "-m", "agent_platform", "claude", "watch", "--interval", str(interval)]
    env = {name: environ[name] for name in PASSED_ENV if environ.get(name)}
    if platform == "darwin":
        args = "".join(f"<string>{_xml(arg)}</string>" for arg in argv)
        variables = "".join(f"<key>{name}</key><string>{_xml(value)}</string>" for name, value in env.items())
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            f"<key>Label</key><string>{LAUNCHD_LABEL}</string>"
            f"<key>ProgramArguments</key><array>{args}</array>"
            f"<key>EnvironmentVariables</key><dict>{variables}</dict>"
            "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            "</dict></plist>\n"
        )
    quoted = " ".join(json.dumps(arg) for arg in argv)
    lines = ["[Unit]", "Description=agent-platform: keep the Claude login of this computer current", "", "[Service]"]
    lines += [f"Environment={json.dumps(f'{name}={value}')}" for name, value in env.items()]
    lines += [f"ExecStart={quoted}", "Restart=always", "RestartSec=60", "", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def load_command(path: Path, install: bool, platform: str = sys.platform) -> list[str]:
    if platform == "darwin":
        domain = f"gui/{os.getuid()}"
        return ["launchctl", "bootstrap", domain, str(path)] if install else ["launchctl", "bootout", domain, str(path)]
    action = ["enable", "--now"] if install else ["disable", "--now"]
    return ["systemctl", "--user", *action, path.name]


def run_loader(argv: list[str]) -> bool:
    """Load or unload the service (replaced in tests: services are never loaded there)."""
    try:
        return subprocess.run(argv, capture_output=True, check=False, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_service(environ: Mapping[str, str], interval: int, load: bool) -> dict[str, Any]:
    path = service_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(service_text(environ, interval), encoding="utf-8")
    loaded = run_loader(load_command(path, True)) if load else False
    return {"path": str(path), "loaded": loaded}


def remove_service(environ: Mapping[str, str], load: bool) -> dict[str, Any]:
    path = service_path(environ)
    unloaded = run_loader(load_command(path, False)) if load and path.exists() else False
    existed = path.exists()
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return {"path": str(path), "removed": existed, "unloaded": unloaded}


# --- setup token ---


def claude_program(environ: Mapping[str, str]) -> str:
    configured = environ.get("AGENT_PLATFORM_CLAUDE", "")
    found = configured or shutil.which("claude", path=environ.get("PATH"))
    if not found or not os.access(found, os.X_OK):
        raise PlatformError("claude_cli_missing")
    return found


async def _run_setup_token(program: str, environ: Mapping[str, str], cwd: Path, timeout: float) -> str:
    try:
        process = PtyProcess.spawn([program, "setup-token"], environ, cwd)
    except SpawnFailed:
        raise PlatformError("claude_cli_missing") from None
    deadline = process.loop.time() + timeout
    try:
        while True:
            lines = process.screen.lines()
            token = screen_labels.parse_token(lines)
            if token is not None:
                return token
            if screen_labels.shown(lines, screen_labels.ERROR_LABEL) or process.eof:
                raise PlatformError("setup_token_failed")
            remaining = deadline - process.loop.time()
            if remaining <= 0:
                raise PlatformError("setup_token_failed")
            await process.wait_change(min(remaining, 1.0))
    finally:
        process.kill()


def setup_token(account: Account, environ: Mapping[str, str], home: Home) -> dict[str, Any]:
    program = claude_program(environ)
    print("agent-platform: `claude setup-token` opens a page; the user clicks Authorize there", file=sys.stderr)
    home.ensure_dir()
    token = asyncio.run(_run_setup_token(program, environ, home.path, SETUP_TIMEOUT_S))
    result = store(account, {"token": token, "email": None, "kind": SETUP_TOKEN}, replace=True)
    return {**result, "kind": SETUP_TOKEN}
