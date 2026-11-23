"""Fixtures of the agent-tool suites: the hub fake, a tool environment with stand-ins for the browser and the
`security` program (no real keychain, profile or browser is touched), and the browser side of a pairing."""

from __future__ import annotations

import http.client
import io
import json
import os
import secrets
import stat
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from agent_platform import cli
from agent_runner.crypto.encoding import b64u_encode

from .hubfake import HubFake, User

# `security` stand-in: a file per item in a directory; `-i` reads one command from stdin (as the tool writes it).
SECURITY = """#!{python}
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
store = os.path.join(here, "keychain")
os.makedirs(store, exist_ok=True)
with open(os.path.join(here, "security.log"), "a") as log:
    log.write(" ".join(a for a in sys.argv[1:] if a != "-w") + "\\n")
if os.path.exists(os.path.join(here, "security.fail")):
    sys.exit(51)
args = sys.argv[1:]
if args[:1] == ["-i"]:
    args = sys.stdin.read().split()
def opt(name):
    return args[args.index(name) + 1] if name in args else ""
item = os.path.join(store, opt("-s") + "__" + opt("-a"))
if args[0] == "add-generic-password":
    with open(item, "w") as handle:
        handle.write(opt("-w"))
    sys.exit(0)
if args[0] == "find-generic-password":
    if os.path.exists(item):
        sys.stdout.write(open(item).read())
        sys.exit(0)
    sys.stderr.write("security: The specified item could not be found in the keychain.\\n")
    sys.exit(44)
if args[0] == "delete-generic-password":
    if os.path.exists(item):
        os.remove(item)
        sys.exit(0)
    sys.exit(44)
sys.exit(2)
"""
BROWSER = '#!/bin/sh\nprintf "%s\\n" "$1" >> "$(dirname "$0")/opened.txt"\n'


@dataclass
class Tool:
    """The environment of one computer and helpers to run the tool on it."""

    root: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def home(self) -> Path:
        return Path(self.env["AGENT_PLATFORM_HOME"])

    @property
    def keychain(self) -> Path:
        return self.root / "bin" / "keychain"

    @property
    def security_log(self) -> Path:
        return self.root / "bin" / "security.log"

    def run(self, *argv: str, stdin: str = "", env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        out = io.StringIO()
        code = cli.run(list(argv), {**self.env, **(env or {})}, io.StringIO(stdin), out)
        text = out.getvalue()
        assert text.count("\n") == 1, text
        return code, json.loads(text)

    def opened(self) -> list[str]:
        path = self.root / "bin" / "opened.txt"
        return path.read_text().splitlines() if path.exists() else []

    def claude_keychain(self, document: object) -> None:
        self.keychain.mkdir(exist_ok=True)
        (self.keychain / "Claude Code-credentials__").write_text(json.dumps(document))

    def claude_file(self, document: object) -> Path:
        path = Path(self.env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document) if not isinstance(document, str) else document)
        return path


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def make_tool(root: Path) -> Tool:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    _executable(bin_dir / "security", SECURITY.format(python=sys.executable))
    _executable(bin_dir / "browser", BROWSER)
    env = {
        "AGENT_PLATFORM_HOME": str(root / "home"),
        "AGENT_PLATFORM_BROWSER": str(bin_dir / "browser"),
        "AGENT_PLATFORM_SECURITY": str(bin_dir / "security"),
        "AGENT_PLATFORM_SERVICE_DIR": str(root / "services"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "PATH": os.environ.get("PATH", ""),
    }
    return Tool(root, env)


@pytest.fixture
def hub() -> Iterator[HubFake]:
    fake = HubFake().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def tool(tmp_path: Path) -> Tool:
    return make_tool(tmp_path / "computer")


def wait_for(predicate: Any, seconds: float = 10.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("timed out")


def fragment(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).fragment).items()}


def post_form(
    port: int,
    fields: dict[str, str] | str,
    *,
    host: str | None = None,
    method: str = "POST",
    path: str = "/agent-pairing",
    content_type: str = "application/x-www-form-urlencoded",
) -> tuple[int, dict[str, str]]:
    """The browser's top-level form POST to the loopback listener (or a variant of it)."""
    body = (fields if isinstance(fields, str) else urlencode(fields)).encode()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{port}")
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        response.read()
        return response.status, dict(response.getheaders())
    finally:
        connection.close()


@dataclass
class PairRun:
    thread: threading.Thread
    result: dict[str, Any] = field(default_factory=dict)

    def join(self) -> dict[str, Any]:
        self.thread.join(30)
        return self.result


def start_pair(tool: Tool, hub: HubFake, *extra: str) -> tuple[PairRun, dict[str, str]]:
    """`pair` in a thread; returns the run and the fragment of the link it opened."""
    seen = len(tool.opened())
    run = PairRun(threading.Thread(target=lambda: None))

    def target() -> None:
        code, document = tool.run("pair", "--hub", hub.origin, "--name", "Claude Code", "--host", "mac", *extra)
        run.result.update(document, rc=code)

    run.thread = threading.Thread(target=target, daemon=True)
    run.thread.start()
    url = wait_for(lambda: tool.opened()[seen:] and tool.opened()[seen])
    return run, fragment(url)


def browser_confirms(hub: HubFake, user: User, link: dict[str, str]) -> dict[str, str]:
    """Confirm the pairing as the browser and return the form fields of the handover."""
    secret = secrets.token_bytes(32)
    hub.confirm(link["p"], user, secret)
    return {"p": link["p"], "r": b64u_encode(secret), "k": hub.root_text, "u": user.id}


def pair_user(tool: Tool, hub: HubFake, user: User) -> dict[str, Any]:
    run, link = start_pair(tool, hub)
    status, headers = post_form(int(link["c"]), browser_confirms(hub, user, link))
    assert status == 303
    assert headers["Location"] == f"{hub.origin}/settings?agent=connected"
    result = run.join()
    assert result["ok"] is True, result
    return result


@pytest.fixture
def account(tool: Tool, hub: HubFake) -> User:
    """A user with a private organization and this computer's agent paired to it."""
    user = hub.add_user()
    pair_user(tool, hub, user)
    return user
