"""Fixtures of the gateway suite (GST-04): a real model double (DBL-01) as upstream, credential provisioning through
the control socket, and an HTTP client for the API port that speaks as Claude Code does.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.model.access import Access
from agent_doubles.model.scenario import parse_scenario
from agent_doubles.model.server import ModelDouble
from agent_guest import sessions
from agent_guest.control import ControlContext
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, ControlClient, Reply

DEEPSEEK = "deepseek/deepseek-v4.1-flash-1017"
OPUS = "claude-opus-5-5"
UPSTREAM_KEY = "test-api-key-not-real-0123456789abcdef"
ROUTER_KEY = "test-router-key-not-real-0123456789abcdef"
OAUTH_TOKEN = "test-oauth-token-not-real-0123456789abcdef"
SECRETS = (UPSTREAM_KEY, ROUTER_KEY, OAUTH_TOKEN, SESSION_TOKEN)
# Guest access kind -> (double access kind, upstream token, auth header, auth value, model).
KINDS: dict[str, tuple[str, str, str, str, str]] = {
    "api_key": ("api_key", UPSTREAM_KEY, "x-api-key", UPSTREAM_KEY, OPUS),
    "platform": ("openrouter", ROUTER_KEY, "authorization", f"Bearer {ROUTER_KEY}", DEEPSEEK),
    "subscription": ("subscription", OAUTH_TOKEN, "authorization", f"Bearer {OAUTH_TOKEN}", OPUS),
}
CLAUDE_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
    "x-app": "cli",
    "user-agent": "claude-cli/2.1.330 (external, sdk-cli)",
    "Content-Type": "application/json",
}


def text_step(text: str = "Hello.", **reply: Any) -> dict[str, Any]:
    # deterministic and collision-free per text (`hash()` is randomized per process and `% 1000` collided)
    return {
        "id": "s" + hashlib.sha256(text.encode()).hexdigest()[:12],
        "reply": {"content": [{"type": "text", "text": text}], **reply},
    }


def messages(model: str, *, stream: bool = True, text: str = "Say hello.") -> dict[str, Any]:
    return {"model": model, "max_tokens": 32000, "stream": stream, "messages": [{"role": "user", "content": text}]}


@dataclass
class Started:
    double: ModelDouble
    client: DoubleClient

    @property
    def url(self) -> str:
        return self.double.url


@pytest.fixture
def start_double() -> Iterator[Callable[..., Started]]:
    """Start a model double for an access kind with a raw scenario (default: one text answer)."""
    started: list[ModelDouble] = []

    def start(kind: str = "api_key", *steps: dict[str, Any], openrouter: dict[str, Any] | None = None) -> Started:
        double_kind, token = KINDS[kind][0], KINDS[kind][1]
        raw: dict[str, Any] = {"name": "gateway", "steps": list(steps) or [text_step()]}
        if openrouter is not None or kind == "platform":
            raw["openrouter"] = openrouter or {"provider": "DeepInfra"}
        double = ModelDouble(parse_scenario(raw), access=Access(double_kind, token)).start()
        started.append(double)
        return Started(double, DoubleClient(double.url, timeout=10))

    yield start
    for double in reversed(started):
        double.stop()


def credential_body(
    kind: str,
    upstream: str,
    *,
    models: Mapping[str, int] | None = None,
    budget: dict[str, float] | None = None,
    providers: list[str] | None = None,
    extra: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    _, _, header, value, model = KINDS[kind]
    body: dict[str, Any] = {
        "upstream_base_url": upstream,
        "auth_header": header,
        "auth_value": value,
        "extra_headers": extra or [],
        "access": kind,
        "models_allowed": [
            {"model_id": name, "context_window": window}
            for name, window in (models if models is not None else {model: 1_000_000}).items()
        ],
    }
    if budget is not None:
        body["budget"] = budget
    if providers is not None:
        body["provider_allowlist"] = providers
    return body


@pytest.fixture(autouse=True)
def started_agents(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records the agent starts the provisioning asks GST-05 for (the session manager needs Linux and root)."""
    calls: list[str] = []

    def record(context: ControlContext, session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(sessions, "credential_provisioned", record)
    return calls


@pytest.fixture
def provision(control_client: ControlClient, session: Any) -> Callable[..., Reply]:
    """``PUT /v1/sessions/{id}/credential`` for the working session; asserts ``200 {}``."""

    def put(kind: str, upstream: str, *, session_id: str = SESSION_ID, **options: Any) -> Reply:
        reply = control_client.request(
            "PUT", f"/v1/sessions/{session_id}/credential", credential_body(kind, upstream, **options)
        )
        assert (reply.status, reply.json()) == (200, {}), reply.body
        return reply

    return put


@dataclass
class Answer:
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class Api:
    """Client of port 7700 with the session token as Claude Code sends it (``Authorization: Bearer``)."""

    def __init__(self, port: int) -> None:
        self.port = port

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)

    def request(
        self,
        method: str,
        target: str,
        body: object = None,
        *,
        raw: bytes | None = None,
        token: str | None = SESSION_TOKEN,
        via: str = "bearer",
        headers: Mapping[str, str] | None = None,
    ) -> Answer:
        sent = dict(CLAUDE_HEADERS)
        sent.update(headers or {})
        if token is not None:
            if via == "bearer":
                sent["Authorization"] = f"Bearer {token}"
            else:
                sent["x-api-key"] = token
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        connection = self.connection()
        try:
            connection.request(method, target, body=data, headers=sent)
            response = connection.getresponse()
            return Answer(response.status, response.headers, response.read())
        finally:
            connection.close()


@pytest.fixture
def api(api_port: int) -> Api:
    return Api(api_port)


def journal_bodies(context: ControlContext, kind: str) -> list[tuple[str | None, dict[str, Any]]]:
    records, _ = context.journal.read(0, 1000)
    return [(record["session_id"], record["body"]) for record in records if record["kind"] == kind]


def all_journal_text(context: ControlContext) -> str:
    records, _ = context.journal.read(0, 1000)
    return json.dumps(records)


def sse_events(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in raw.decode().split("\n\n"):
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((name, data))
    return events


class RawUpstream:
    """A hand-written HTTP upstream for failure shapes the model double does not produce.

    ``hold``: SSE headers and ``message_start``, then (after ``release``) more chunks until the gateway hangs up
    (``closed`` is set). ``cut``: a chunk shorter than announced, then the connection drops. ``silent``: reads the
    request and closes without an answer. ``plain``: a JSON answer without ``Content-Type`` and ``request-id``, with a
    repeated header, a cookie and a cache header.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[bytes] = []
        self.released = threading.Event()
        self.closed = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.listener.getsockname()[1]}"

    def release(self) -> None:
        self.released.set()

    def close(self) -> None:
        self.released.set()
        self.listener.close()
        self.thread.join(10)

    def _read_request(self, connection: socket.socket) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            data += connection.recv(65536)
        head, _, body = data.partition(b"\r\n\r\n")
        length = int(
            next(line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length"))
        )
        while len(body) < length:
            body += connection.recv(65536)
        self.requests.append(data)

    def _serve(self) -> None:
        try:
            connection, _ = self.listener.accept()
        except OSError:
            return
        with connection:
            self._read_request(connection)
            getattr(self, f"_{self.mode}")(connection)

    def _hold(self, connection: socket.socket) -> None:
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}}
        event = f"event: message_start\ndata: {json.dumps(start)}\n\n".encode()
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nrequest-id: req_raw_1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n" + b"%x\r\n%s\r\n" % (len(event), event)
        )
        self.released.wait(10)
        ping = b'event: ping\ndata: {"type":"ping"}\n\n'
        try:
            for _ in range(400):
                connection.sendall(b"%x\r\n%s\r\n" % (len(ping), ping))
                time.sleep(0.025)
        except OSError:
            self.closed.set()

    def _cut(self, connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n64\r\nevent: mes"
        )
        time.sleep(0.2)

    def _silent(self, connection: socket.socket) -> None:
        return

    def _plain(self, connection: socket.socket) -> None:
        body = json.dumps({"type": "message", "usage": {"input_tokens": 3}}).encode()
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nX-Dup: a\r\nX-Dup: b\r\nSet-Cookie: s=1\r\nCache-Control: max-age=5\r\n"
            b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body)
        )


@pytest.fixture
def raw_upstream() -> Iterator[Callable[..., RawUpstream]]:
    started: list[RawUpstream] = []

    def start(*, hold: bool = False, cut: bool = False, silent: bool = False, plain: bool = False) -> RawUpstream:
        mode = next(name for name, on in (("hold", hold), ("cut", cut), ("silent", silent), ("plain", plain)) if on)
        upstream = RawUpstream(mode)
        started.append(upstream)
        return upstream

    yield start
    for upstream in started:
        upstream.close()
