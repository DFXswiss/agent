"""U-RUN-sync (HTTP connections, KOR-18): the keep-alive race between the runner's client and a hub that closes idle
connections (05-runner.md section 2.3).

A real HTTP/1.1 server with a small idle limit sits behind a relay that delays every byte in both directions, like a
route with a noticeable round trip. A request sent shortly before the server's idle limit (as the server sees it)
travels while the server already closes the connection: without the fix the request is lost (`hub_unreachable`);
with it the client either no longer reuses such a connection or sends the request once more on a new one. Answers
that do not verify are never repeated, failures on a new connection and failures after the answer began neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_cli.keys import response_text
from agent_runner.encoding import b64u_encode
from agent_runner.sync import http
from agent_runner.sync.http import HubClient, HubTransportError, format_time, sha256_hex

IDLE = 0.6
"""Idle limit of the test server (seconds after its last answer)."""
DELAY = 0.2
"""One-way delay of the relay."""
IN_WINDOW = IDLE - DELAY
"""Client wait after an answer: the request reaches the server after its idle limit, the server's close reaches the
client only after the request was sent (both with a margin of `DELAY`)."""
KEY_ID = "device." + "ab" * 16


@dataclass
class Server:
    """HTTP/1.1 keep-alive server with an idle limit; answers `{"n": <count>}` signed by `hub_key`."""

    hub_key: Ed25519PrivateKey
    idle: float = IDLE
    behaviours: list[str] = field(default_factory=list)
    """Per request, in order: `ok`, `close` (close without an answer), `truncate` (headers, then close),
    `bad_signature`; `ok` once the list is empty."""
    requests: list[tuple[int, str, dict[str, str]]] = field(default_factory=list)
    connections: int = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        number = self.connections
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.idle)
                except (TimeoutError, asyncio.IncompleteReadError):
                    return
                lines = head.decode("ascii").split("\r\n")
                headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                self.requests.append((number, lines[0], headers))
                behaviour = self.behaviours.pop(0) if self.behaviours else "ok"
                if behaviour == "close":
                    return
                answer = json.dumps({"n": len(self.requests), "echo": sha256_hex(body)}).encode()
                hub_time = format_time(time.time())
                text = response_text(
                    headers["agent-key"],
                    headers["agent-nonce"],
                    headers["agent-time"],
                    200,
                    hub_time,
                    sha256_hex(answer),
                )
                signature = self.hub_key.sign(text.encode())
                if behaviour == "bad_signature":
                    signature = bytes(64)
                head_out = (
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(answer)}\r\nAgent-Hub-Key: hub-1\r\nAgent-Hub-Time: {hub_time}\r\n"
                    f"Agent-Hub-Signature: {b64u_encode(signature)}\r\n\r\n"
                ).encode()
                if behaviour == "truncate":
                    writer.write(head_out + answer[:3])
                    await writer.drain()
                    return
                writer.write(head_out + answer)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, delay: float) -> None:
    """Copy bytes, each chunk `delay` after it arrived (order kept); the end of the stream is delayed alike."""
    queue: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue()

    async def deliver() -> None:
        while True:
            chunk, due = await queue.get()
            await asyncio.sleep(max(0.0, due - asyncio.get_running_loop().time()))
            if not chunk:
                return
            writer.write(chunk)
            with contextlib.suppress(OSError):
                await writer.drain()

    sender = asyncio.ensure_future(deliver())
    try:
        while True:
            try:
                chunk = await reader.read(65536)
            except OSError:
                chunk = b""
            await queue.put((chunk, asyncio.get_running_loop().time() + delay))
            if not chunk:
                break
        await sender
    finally:
        sender.cancel()
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@contextlib.asynccontextmanager
async def route(server: Server, delay: float = DELAY) -> AsyncIterator[str]:
    """Start the server and a delaying relay in front of it; the relay's base URL."""
    backend = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = backend.sockets[0].getsockname()[1]

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.gather(_pipe(reader, up_writer, delay), _pipe(up_reader, writer, delay))

    front = await asyncio.start_server(relay, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{front.sockets[0].getsockname()[1]}"
    finally:
        front.close()
        backend.close()


def _client(url: str, hub_key: Ed25519PrivateKey, **extra: Any) -> HubClient:
    public = hub_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return HubClient(hub_url=url, key_id=KEY_ID, signing_key=Ed25519PrivateKey.generate(), hub_key=public, **extra)


def run(body: Callable[[], Any]) -> None:
    asyncio.run(body())


async def _two_requests(server: Server, wait: float, **extra: Any) -> tuple[dict[str, object], dict[str, object]]:
    async with route(server) as url:
        client = _client(url, server.hub_key, **extra)
        try:
            first = await client.request("POST", "/v1/device/outbox", {"items": []})
            await asyncio.sleep(wait)
            second = await client.request("POST", "/v1/device/outbox", {"items": []})
        finally:
            await client.aclose()
    return first, second


def test_the_race_is_reproduced_on_a_reused_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second request is sent on the connection the server closes meanwhile; the fix sends it once more with a new
    signature and the same body (without the fix: `hub_unreachable`). The spy proves the repeat ran: a client that
    overslept the window would find the connection closed and pass without one."""
    server = Server(Ed25519PrivateKey.generate())
    attempts: list[str] = []
    send = HubClient._send

    async def spy(self: HubClient, method: str, target: str, raw: bytes, json_body: bool) -> Any:
        try:
            return await send(self, method, target, raw, json_body)
        except HubTransportError as error:
            attempts.append(error.describe())
            raise

    monkeypatch.setattr(HubClient, "_send", spy)

    async def body() -> None:
        first, second = await _two_requests(server, IN_WINDOW, keepalive_expiry=5.0)
        assert first["n"] == 1
        assert second["n"] == 2
        assert first["echo"] == second["echo"]
        assert server.connections == 2
        assert attempts == ["reason=hub_unreachable reused=yes cause=RemoteProtocolError"]
        assert [number for number, _line, _headers in server.requests] == [1, 2]
        assert server.requests[0][2]["agent-nonce"] != server.requests[1][2]["agent-nonce"]

    run(body)


def test_the_default_idle_time_closes_before_a_five_second_server() -> None:
    """Without an explicit value the client reuses an idle connection for `KEEPALIVE_EXPIRY_S` only: a connection idle
    for longer is replaced by a new one, although the server would still keep it (httpx's default of 5 s would reuse
    it and so meet a 5 s server exactly at its limit)."""
    assert http.KEEPALIVE_EXPIRY_S <= 2.0
    server = Server(Ed25519PrivateKey.generate(), idle=10.0)

    async def body() -> None:
        async with route(server, delay=0.0) as url:
            client = _client(url, server.hub_key)
            try:
                await client.request("GET", "/v1/device/state")
                await asyncio.sleep(http.KEEPALIVE_EXPIRY_S + 0.3)
                await client.request("GET", "/v1/device/state")
            finally:
                await client.aclose()
        assert server.connections == 2
        assert [number for number, _line, _headers in server.requests] == [1, 2]

    run(body)


def test_a_reused_connection_within_the_idle_time_is_kept() -> None:
    server = Server(Ed25519PrivateKey.generate())

    async def body() -> None:
        first, second = await _two_requests(server, 0.0)
        assert (first["n"], second["n"]) == (1, 2)
        assert server.connections == 1

    run(body)


def test_the_repeat_happens_once_only() -> None:
    """Request 2 and its repeat both meet a server closing without an answer: `hub_unreachable`, reused connection
    named; a third connection is never opened for it."""
    server = Server(Ed25519PrivateKey.generate(), behaviours=["ok", "close", "close"])

    async def body() -> None:
        async with route(server, delay=0.0) as url:
            client = _client(url, server.hub_key)
            try:
                await client.request("GET", "/v1/device/state")
                with pytest.raises(HubTransportError) as raised:
                    await client.request("GET", "/v1/device/state")
            finally:
                await client.aclose()
        assert raised.value.reason == "hub_unreachable"
        assert raised.value.reused is False
        assert raised.value.describe() == f"reason=hub_unreachable reused=no cause={raised.value.cause}"
        assert len(server.requests) == 3
        assert server.connections == 2

    run(body)


def test_a_failure_on_a_new_connection_is_not_repeated() -> None:
    server = Server(Ed25519PrivateKey.generate(), behaviours=["close"])

    async def body() -> None:
        async with route(server, delay=0.0) as url:
            client = _client(url, server.hub_key)
            try:
                with pytest.raises(HubTransportError) as raised:
                    await client.request("GET", "/v1/device/state")
            finally:
                await client.aclose()
        assert raised.value.reused is False
        assert raised.value.cause == "RemoteProtocolError"
        assert len(server.requests) == 1

    run(body)


def test_a_failure_after_the_answer_began_is_not_repeated() -> None:
    server = Server(Ed25519PrivateKey.generate(), behaviours=["ok", "truncate"])

    async def body() -> None:
        async with route(server, delay=0.0) as url:
            client = _client(url, server.hub_key)
            try:
                await client.request("GET", "/v1/device/state")
                with pytest.raises(HubTransportError) as raised:
                    await client.request("GET", "/v1/device/state")
            finally:
                await client.aclose()
        assert raised.value.reason == "hub_unreachable"
        assert raised.value.reused is True
        assert "reused=yes" in raised.value.describe()
        assert len(server.requests) == 2

    run(body)


def test_an_answer_that_does_not_verify_is_never_repeated() -> None:
    """A wrongly signed answer on a reused connection stays an error; the request is not sent again."""
    server = Server(Ed25519PrivateKey.generate(), behaviours=["ok", "bad_signature"])

    async def body() -> None:
        async with route(server, delay=0.0) as url:
            client = _client(url, server.hub_key)
            try:
                await client.request("GET", "/v1/device/state")
                with pytest.raises(HubTransportError) as raised:
                    await client.request("GET", "/v1/device/state")
            finally:
                await client.aclose()
        assert raised.value.reason == "response_signature_invalid"
        assert raised.value.describe() == "reason=response_signature_invalid"
        assert len(server.requests) == 2

    run(body)


def test_a_transport_without_trace_events_is_never_repeated() -> None:
    """A transport that reports no connection events (a test or custom transport): unknown reuse, no repeat."""
    calls: list[httpx.Request] = []

    def broken(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    async def body() -> None:
        client = _client("http://hub.test", Ed25519PrivateKey.generate(), transport=httpx.MockTransport(broken))
        try:
            with pytest.raises(HubTransportError) as raised:
                await client.request("GET", "/v1/device/state")
        finally:
            await client.aclose()
        assert raised.value.reused is None
        assert raised.value.describe() == "reason=hub_unreachable cause=RemoteProtocolError"
        assert len(calls) == 1

    run(body)
