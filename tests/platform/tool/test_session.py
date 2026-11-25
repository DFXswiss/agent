"""The agent tool as the main orchestrator inside a platform session (ADR-082 sections 6 and 9.7-9.8, 05-runner.md
sections 17.2, 17.4a and 17.5): the relay transport, `pair --session` with its handover, the commands refused in
session mode and the MCP server on a unix socket.

The guest's `orch.sock` and the runner are played by `RelayFake`: a unix socket that forwards each relay line to the
hub fake unchanged and returns the hub's answer with the headers the runner passes on."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import io
import json
import os
import secrets
import shutil
import signal
import socket
import socketserver
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_platform import cli, connector, relay, session
from agent_platform.errors import NEXT, PlatformError
from agent_platform.hub import AgentClient, anon_request
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.ids import uuid7

from .conftest import Tool, make_tool, wait_for
from .hubfake import HUB_KEY_ID, AgentRow, HubFake, User

SESSION = "0193000a-0000-7000-8000-00000000000a"
PASSED = {"agent-hub-key", "agent-hub-time", "agent-hub-signature", "content-type", "retry-after"}
Answer = Callable[[dict[str, Any]], bytes | None]


class RelayFake:
    """`orch.sock` with the runner behind it: each line is forwarded to the hub unchanged (or answered by `answer`)."""

    def __init__(self, path: str, hub: HubFake) -> None:
        self.path = path
        self.hub = hub
        self.lines: list[dict[str, Any]] = []
        self.answer: Answer | None = None
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                message = json.loads(self.rfile.readline())
                fake.lines.append(message)
                special = None if fake.answer is None else fake.answer(message)
                self.wfile.write(special if special is not None else fake.forward(message["args"]))

        self.server = socketserver.ThreadingUnixStreamServer(path, Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def forward(self, args: dict[str, Any]) -> bytes:
        connection = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=10)
        try:
            body = b64u_decode(args["body"]) if args["body"] else b""
            connection.request(args["method"], args["target"], body=body or None, headers=args["headers"])
            response = connection.getresponse()
            data = response.read()
            headers = {name: value for name, value in response.getheaders() if name.lower() in PASSED}
            result = {"status": response.status, "headers": headers, "body": b64u_encode(data) if data else ""}
        finally:
            connection.close()
        return (json.dumps({"id": 1, "ok": True, "result": result}) + "\n").encode()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="tses", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def relay_fake(short_dir: Path, hub: HubFake) -> Iterator[RelayFake]:
    fake = RelayFake(str(short_dir / "orch.sock"), hub)
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def orch(tmp_path: Path, relay_fake: RelayFake) -> Tool:
    """The tool as the guest runs it: session mode with the relay."""
    found = make_tool(tmp_path / "vm")
    found.env[relay.RELAY_ENV] = relay_fake.path
    return found


def keys_input(hub: HubFake, **change: Any) -> str:
    public = b64u_encode(hub.hub_key.public_key().public_bytes_raw())
    value = {
        "session_id": SESSION,
        "hub_url": hub.origin,
        "hub_key": {"id": HUB_KEY_ID, "public_key": public},
        "platform_root_key": hub.root_text,
        **change,
    }
    return json.dumps(value)


def start(orch: Tool, hub: HubFake) -> dict[str, Any]:
    code, document = orch.run("pair", "--session", stdin=keys_input(hub))
    assert code == 0, document
    return document


def register(hub: HubFake, started: dict[str, Any]) -> None:
    """The runner's registration at the hub (the hub fake keeps it as an open pairing)."""
    pub = started["pub"]
    row = AgentRow(started["pairing_id"], b64u_decode(pub["kem"]), b64u_decode(pub["sig"]), expires=time.time() + 600)
    hub.agents[row.id] = row


def approve(hub: HubFake, user: User, started: dict[str, Any], **change: Any) -> str:
    """The browser confirms and seals the handover to the session's key; the stdin of `--handover`."""
    register(hub, started)
    hub.confirm_remote(started["pairing_id"], user)
    value = {
        "pairing_id": started["pairing_id"],
        "ct": hub.agents[started["pairing_id"]].handover_ct,
        "user_id": user.id,
        "user_pub": user.identity.public(),
        **change,
    }
    return json.dumps(value)


def handover(orch: Tool, text: str) -> tuple[int, dict[str, Any]]:
    return orch.run("pair", "--session", "--handover", stdin=text)


# --- pair --session -------------------------------------------------------------------------------------------


def test_the_session_pairing_claims_through_the_relay_and_works_afterwards(
    orch: Tool, hub: HubFake, relay_fake: RelayFake
) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    assert started["state"] == "waiting"
    assert set(started) == {"ok", "state", "pairing_id", "pub", "pop", "fp", "next"}
    pending = json.loads((orch.home / "pending.json").read_text())
    assert (pending["kind"], pending["session_id"]) == ("session", SESSION)
    agent = session._restore(Home.from_env(orch.env), pending).agent
    assert agent.public()["kem"] == started["pub"]["kem"]
    kem, sig = b64u_decode(started["pub"]["kem"]), b64u_decode(started["pub"]["sig"])
    agent_crypto.verify_pop(started["pairing_id"], kem, sig, started["pop"])
    assert started["fp"] == agent_crypto.fingerprint(kem, sig)
    private = (orch.home / "pending.key").read_text().strip()
    assert private not in json.dumps(started)
    assert stat.S_IMODE((orch.home / "pending.key").stat().st_mode) == 0o600
    assert stat.S_IMODE(orch.home.stat().st_mode) == 0o700

    code, done = handover(orch, approve(hub, user, started))
    assert code == 0, done
    # `keys` (KOR-67): the connected agent's keys stay in the session's own home in the VM
    assert done == {
        "ok": True,
        "state": "connected",
        "agent_id": started["pairing_id"],
        "user_id": user.id,
        "keys": str(orch.home / "agent.key"),
    }
    assert not (orch.home / "pending.json").exists()
    assert not (orch.home / "pending.key").exists()
    assert hub.agents[started["pairing_id"]].state == "active"
    targets = [line["args"]["target"] for line in relay_fake.lines]
    assert targets == ["/v1/agent/claim", "/v1/me"]
    assert all(line["op"] == "relay" and line["id"] == 1 for line in relay_fake.lines)

    code, status = orch.run("status")
    assert code == 0, status
    assert (status["agent_id"], status["user_id"]) == (started["pairing_id"], user.id)
    assert len(relay_fake.lines) > 2


def test_a_new_session_pairing_replaces_a_pending_one(orch: Tool, hub: HubFake) -> None:
    first = start(orch, hub)
    second = start(orch, hub)
    assert first["pairing_id"] != second["pairing_id"]
    assert json.loads((orch.home / "pending.json").read_text())["pairing_id"] == second["pairing_id"]


def test_a_claim_with_other_user_keys_than_the_runners_is_refused(orch: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    other = agent_crypto.new_agent().public()
    code, done = handover(orch, approve(hub, user, started, user_pub=other))
    assert (code, done["error"], done["reason"]) == (1, "pairing_failed", "identity_mismatch")
    assert not (orch.home / "pending.json").exists()
    assert not (orch.home / "state.json").exists()


def test_a_handover_of_another_pairing_or_one_that_does_not_open_is_refused(orch: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    text = approve(hub, user, started, pairing_id=uuid7())
    assert handover(orch, text)[1]["reason"] == "pairing_mismatch"
    assert handover(orch, text)[1]["error"] == "no_pending_pairing"
    started = start(orch, hub)
    text = approve(hub, user, started, ct=b64u_encode(secrets.token_bytes(80)))
    assert handover(orch, text)[1]["reason"] == "handover_invalid"


def test_a_handover_under_another_platform_root_is_refused(orch: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    code, started = orch.run("pair", "--session", stdin=keys_input(hub, platform_root_key=b64u_encode(bytes(32))))
    assert code == 0
    assert handover(orch, approve(hub, user, started))[1]["reason"] == "platform_root_mismatch"


def test_a_wrong_secret_and_a_lost_claim_fail_the_pairing(orch: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    text = approve(hub, user, started)
    row = hub.agents[started["pairing_id"]]
    row.handover_ct = agent_crypto.seal_handover(row.kem, row.id, user.id, secrets.token_bytes(32), hub.root_text)
    text = json.dumps({**json.loads(text), "ct": row.handover_ct})
    assert handover(orch, text)[1]["reason"] == "claim_invalid"
    started = start(orch, hub)
    text = approve(hub, user, started)
    hub.override("r_claim", lambda request, body: (404, {"error": "pairing_unknown"}))
    assert handover(orch, text)[1]["reason"] == "claim_lost"


def test_a_transient_failure_keeps_the_pending_pairing(orch: Tool, hub: HubFake, relay_fake: RelayFake) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    text = approve(hub, user, started)
    relay_fake.answer = lambda message: b'{"id":1,"ok":false,"error":"hub_unreachable"}\n'
    assert handover(orch, text)[1]["error"] == "hub_unreachable"
    assert (orch.home / "pending.json").exists()
    relay_fake.answer = None
    assert handover(orch, text)[1]["state"] == "connected"


@pytest.mark.parametrize(
    "text",
    [
        "no json",
        "[]",
        '{"session_id": "x"}',
        "BAD_SESSION",
        "BAD_URL",
        "BAD_HUB_KEY",
        "BAD_HUB_KEY_ID",
        "BAD_ROOT",
        "URL_PATH",
    ],
)
def test_pair_session_refuses_a_malformed_input(orch: Tool, hub: HubFake, text: str) -> None:
    changes: dict[str, dict[str, Any]] = {
        "BAD_SESSION": {"session_id": "not-a-uuid"},
        "BAD_URL": {"hub_url": 7},
        "BAD_HUB_KEY": {"hub_key": {"id": HUB_KEY_ID}},
        "BAD_HUB_KEY_ID": {"hub_key": {"id": 1, "public_key": hub.root_text}},
        "BAD_ROOT": {"platform_root_key": "AAEC"},
        "URL_PATH": {"hub_url": hub.origin + "/v1"},
    }
    change = changes.get(text, {})
    stdin = keys_input(hub, **change) if change else text
    code, document = orch.run("pair", "--session", stdin=stdin)
    assert (code, document["error"]) == (1, "invalid_argument")
    assert not (orch.home / "pending.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"pairing_id": "x"},
        {"user_id": 3},
        {"user_pub": {"kem": "AAEC"}},
        {"user_pub": {"kem": "AAEC", "sig": "AAEC"}},
        {"ct": ""},
        {"ct": "A" * 5000},
        {"ct": 5},
        {"extra": 1},
    ],
)
def test_pair_session_handover_refuses_a_malformed_input(orch: Tool, hub: HubFake, change: dict[str, Any]) -> None:
    user = hub.add_user()
    text = approve(hub, user, start(orch, hub), **change)
    assert handover(orch, text)[1]["error"] == "invalid_argument"
    assert (orch.home / "pending.json").exists()


def test_a_handover_needs_a_sound_pending_session_pairing(orch: Tool, hub: HubFake) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    text = approve(hub, user, started)
    pending = json.loads((orch.home / "pending.json").read_text())
    (orch.home / "pending.json").write_text(json.dumps({**pending, "kind": "pair"}))
    assert handover(orch, text)[1]["error"] == "no_pending_pairing"
    (orch.home / "pending.json").write_text(json.dumps({**pending, "label": None}))
    assert handover(orch, text)[1]["error"] == "state_damaged"
    (orch.home / "pending.json").write_text(json.dumps({**pending, "platform_root_key": "AAEC"}))
    assert handover(orch, text)[1]["error"] == "state_damaged"


def test_pair_session_options_and_session_mode(orch: Tool, hub: HubFake, tool: Tool) -> None:
    stdin = keys_input(hub)
    for extra in (["--hub", hub.origin], ["--remote"], ["--wait"], ["--return-code", "X"], ["--message", "hi"]):
        # only the guest's exact command lines are taken in a session (relay.guest_command)
        assert orch.run("pair", "--session", *extra, stdin=stdin)[1]["error"] == "not_in_session"
    assert tool.run("pair", "--handover", stdin=stdin)[1]["error"] == "invalid_argument"
    # outside session mode
    assert tool.run("pair", "--session", stdin=stdin)[1]["error"] == "invalid_argument"
    for extra in (["--remote"], ["--message", "hi"]):
        # options of a pairing on a computer never go with --session
        assert tool.run("pair", "--session", *extra, stdin=stdin)[1]["error"] == "invalid_argument"
    assert tool.run("pair", "--session", "--handover", stdin=stdin)[1]["error"] == "invalid_argument"
    assert tool.run("pair", "--session", stdin=stdin, env={relay.RELAY_ENV: "orch.sock"})[1]["error"] == (
        "invalid_argument"
    )


# --- refused in session mode ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["pair", "--hub", "https://hub.example"],
        ["register", "--display-name", "x"],
        ["recover"],
        ["install-connector"],
        ["mcp"],
        ["claude", "sync"],
        ["claude", "service", "install"],
        ["device", "add"],
        # review KOR-57 b no. 4: an allow-list; no option that writes a path of the tool's account
        ["test", "log", "--session", "s", "--output", "/var/lib/agent-orch/state/state.json"],
        ["test", "log", "--output=/tmp/x"],
        ["install-connector", "--config", "/tmp/x"],
        ["recovery", "new"],
        ["passkey", "add"],
        ["github", "connect", "--token", "x"],
        ["community", "serve"],
        ["agents", "confirm"],
        ["platform", "invite"],
        ["--limit", "5", "claude", "sync"],
        ["mcp", "--socket", "/x", "--extra"],
        ["pair", "--session", "--handover", "--wait"],
        ["--trim"],
    ],
)
def test_commands_without_sense_in_a_session_are_refused(orch: Tool, argv: list[str]) -> None:
    code, document = orch.run(*argv)
    assert (code, document) == (1, {"ok": False, "error": "not_in_session", "next": NEXT["not_in_session"]})


def test_refusal_rules() -> None:
    for allowed in (["task", "list", "--wg", "w"], ["test", "log", "--session", "s"], ["--help"], [], ["org"]):
        assert relay.refused_in_session(allowed) is False
    assert relay.refused_in_session(["mcp", "--socket", "/x"]) is False
    assert relay.refused_in_session(["mcp", "--socket=/x"]) is False
    assert relay.refused_in_session(["pair", "--session"]) is False
    assert relay.refused_in_session(["status"]) is False
    assert relay.refused_in_session(["--version"]) is False


def test_no_anonymous_request_in_session_mode() -> None:
    with relay.session_mode({relay.RELAY_ENV: "/nowhere"}), pytest.raises(PlatformError) as error:
        anon_request("http://127.0.0.1:9", "GET", "/v1/config")
    assert error.value.code == "not_in_session"
    with relay.session_mode({}):
        assert relay.relay_path() is None
        assert relay.transport() is None


def test_the_mcp_run_tool_refuses_pairing_in_a_session(orch: Tool) -> None:
    for argv in (["pair", "--session"], ["pair", "--session", "--handover"], ["claude", "connect"]):
        document = connector.tool_run({"args": argv}, orch.env)
        assert document["error"] == "not_in_session"
    assert connector.tool_run({"args": ["guide"]}, orch.env)["ok"] is True
    assert connector.tool_run({"args": ["mcp"]}, orch.env)["error"] == "invalid_argument"


# --- the relay transport --------------------------------------------------------------------------------------


def fetch(path: str, timeout: float = 5.0, content: bytes | None = None) -> httpx.Response:
    async def go() -> httpx.Response:
        async with httpx.AsyncClient(transport=relay.RelayTransport(path, timeout)) as client:
            response = await client.request(
                "POST" if content else "GET",
                "http://hub.example/v1/x?y=1",
                content=content,
                headers={"Agent-Key": "agent.k", "Cookie": "c", "Content-Type": "application/json"},
            )
            await response.aread()
            return response

    return asyncio.run(go())


def answering(relay_fake: RelayFake, line: bytes | None) -> None:
    relay_fake.answer = lambda message: line


def test_the_transport_sends_the_signed_headers_only(relay_fake: RelayFake) -> None:
    result = {"status": 201, "headers": {"Content-Type": "application/json"}, "body": b64u_encode(b"{}")}
    answering(relay_fake, (json.dumps({"id": 1, "ok": True, "result": result}) + "\n").encode())
    response = fetch(relay_fake.path, content=b'{"a":1}')
    assert (response.status_code, response.content) == (201, b"{}")
    args = relay_fake.lines[-1]["args"]
    assert args == {
        "method": "POST",
        "target": "/v1/x?y=1",
        "headers": {"Agent-Key": "agent.k", "Content-Type": "application/json"},
        "body": b64u_encode(b'{"a":1}'),
    }
    result = {"status": 204, "headers": {}, "body": ""}
    answering(relay_fake, (json.dumps({"id": 1, "ok": True, "result": result}) + "\n").encode())
    response = fetch(relay_fake.path)
    assert (response.status_code, response.content, relay_fake.lines[-1]["args"]["body"]) == (204, b"", "")


@pytest.mark.parametrize(
    "line",
    [
        b"no json\n",
        b"[]\n",
        b'{"id":2,"ok":true}\n',
        b'{"id":1,"ok":false,"error":"refused"}\n',
        b'{"id":1,"ok":true,"result":{"status":200}}\n',
        b'{"id":1,"ok":true,"result":{"status":700,"headers":{},"body":""}}\n',
        b'{"id":1,"ok":true,"result":{"status":true,"headers":{},"body":""}}\n',
        b'{"id":1,"ok":true,"result":{"status":200,"headers":[],"body":""}}\n',
        b'{"id":1,"ok":true,"result":{"status":200,"headers":{"Set-Cookie":"a"},"body":""}}\n',
        b'{"id":1,"ok":true,"result":{"status":200,"headers":{"Content-Type":1},"body":""}}\n',
        b'{"id":1,"ok":true,"result":{"status":200,"headers":{},"body":"!!"}}\n',
        b'{"id":1,"ok":true,"result":{"status":200,"headers":{},"body":5}}\n',
        b"",
    ],
)
def test_a_refused_or_malformed_relay_answer_is_a_transport_error(relay_fake: RelayFake, line: bytes) -> None:
    answering(relay_fake, line)
    with pytest.raises(httpx.TransportError):
        fetch(relay_fake.path)


def test_a_missing_or_silent_relay_is_a_transport_error(relay_fake: RelayFake, short_dir: Path) -> None:
    with pytest.raises(httpx.ConnectError):
        fetch(str(short_dir / "none.sock"))
    release = threading.Event()

    def silent(message: dict[str, Any]) -> bytes:
        release.wait(5)
        return b"\n"

    relay_fake.answer = silent
    try:
        with pytest.raises(httpx.ConnectError):
            fetch(relay_fake.path, timeout=0.2)
    finally:
        release.set()


def test_the_signed_client_still_checks_the_hub_signature(orch: Tool, hub: HubFake, relay_fake: RelayFake) -> None:
    user = hub.add_user()
    started = start(orch, hub)
    assert handover(orch, approve(hub, user, started))[1]["state"] == "connected"
    hub.sign_answers = False
    code, document = orch.run("status")
    assert (code, document["error"], document["reason"]) == (1, "hub_refused", "response_unsigned")
    hub.sign_answers = True
    with relay.session_mode(orch.env):
        client = AgentClient(hub.origin, started["pairing_id"], agent_crypto.new_agent(), bytes(32))
    assert isinstance(client._client._client._transport, relay.RelayTransport)
    client.close()


# --- mcp --socket ---------------------------------------------------------------------------------------------


def request_unix(path: str, method: str, target: str, body: bytes | None = None, length: str | None = None) -> Any:
    connection = http.client.HTTPConnection("localhost", timeout=10)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    connection.sock = sock
    try:
        connection.putrequest(method, target)
        if length is not None:
            connection.putheader("Content-Length", length)
        elif body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        if body is not None:
            with contextlib.suppress(BrokenPipeError):
                connection.send(body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def serve(orch: Tool, path: str, client: Callable[[], Any]) -> Any:
    """Run `mcp --socket` in this thread, `client` in another one, then stop the server with SIGTERM."""
    found: dict[str, Any] = {}

    def run() -> None:
        try:
            wait_for(lambda: os.path.exists(path) and stat.S_ISSOCK(os.lstat(path).st_mode) and os.lstat(path))
            time.sleep(0.05)
            found["value"] = client()
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    with pytest.raises(SystemExit) as stopped:
        cli.run(["mcp", "--socket", path], orch.env, io.StringIO(""), io.StringIO())
    thread.join(10)
    assert stopped.value.code == 0
    assert not os.path.exists(path)
    return found.get("value")


def test_the_mcp_server_on_a_socket(orch: Tool, short_dir: Path) -> None:
    path = str(short_dir / "mcp.sock")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(path)
    stale.close()

    def client() -> list[Any]:
        initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
        arguments = {"args": ["pair", "--session"]}
        run = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "run", "arguments": arguments}}
        return [
            stat.S_IMODE(os.lstat(path).st_mode),
            request_unix(path, "POST", "/mcp", initialize),
            request_unix(path, "POST", "/mcp", b'{"jsonrpc":"2.0","method":"notifications/initialized"}'),
            request_unix(path, "POST", "/mcp", json.dumps(run).encode()),
            request_unix(path, "POST", "/mcp", b"{no json"),
            request_unix(path, "POST", "/mcp", None),
            request_unix(path, "POST", "/mcp", b"", length=str(connector.MAX_BODY + 1)),
            request_unix(path, "POST", "/other", b"{}"),
            request_unix(path, "GET", "/mcp"),
            request_unix(path, "DELETE", "/mcp"),
            request_unix(path, "GET", "/other"),
        ]

    mode, init, note, call, bad, unsized, big, other, get, delete, get_other = serve(orch, path, client)
    assert mode == 0o666
    assert init[0] == 200
    assert json.loads(init[2])["result"]["serverInfo"]["name"] == connector.SERVER
    assert init[1]["Content-Type"] == "application/json"
    assert (note[0], note[2]) == (202, b"")
    assert json.loads(json.loads(call[2])["result"]["content"][0]["text"])["error"] == "not_in_session"
    assert (bad[0], json.loads(bad[2])["error"]["code"]) == (400, connector.PARSE_ERROR)
    assert (unsized[0], json.loads(unsized[2])) == (400, {"error": "request_invalid"})
    assert (big[0], json.loads(big[2])) == (413, {"error": "size_exceeded"})
    assert (other[0], get_other[0]) == (404, 404)
    assert (get[0], get[1]["Allow"], delete[0]) == (405, "POST", 405)


def test_the_mcp_server_creates_its_socket(orch: Tool, short_dir: Path) -> None:
    path = str(short_dir / "fresh.sock")
    ping = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}).encode()
    status, _, body = serve(orch, path, lambda: request_unix(path, "POST", "/mcp", ping))
    assert (status, json.loads(body)["result"]) == (200, {})


def test_the_mcp_socket_path_must_be_absolute_and_free(orch: Tool, short_dir: Path) -> None:
    taken = short_dir / "file"
    taken.write_text("x")
    for path in ("relative.sock", str(taken)):
        code, document = orch.run("mcp", "--socket", path)
        assert (code, document["error"]) == (1, "invalid_argument")
    assert taken.read_text() == "x"
