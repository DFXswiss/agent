"""The agent tool's WebSocket client of the VNC tunnel (`agent_platform.wsclient`, RFC 6455, ADR-101 section 3)
against a real `websockets` server and a scripted raw server on the loopback: the signed upgrade, binary and text
messages, fragments, ping and pong, TLS with the CA of `SSL_CERT_FILE`, every refused handshake and every frame that
closes the socket."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ssl
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from agent_doubles.mail.testca import DoubleCA
from agent_platform import wsclient
from agent_platform.wsclient import MAX_MESSAGE, ClientSocket, HandshakeFailed, accept_of, connect, frame
from agent_runner.vnc.tunnel import SocketClosed

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@asynccontextmanager
async def ws_server(handler: Callable[[ServerConnection], Awaitable[None]], **options: Any) -> AsyncIterator[int]:
    async with serve(handler, "127.0.0.1", 0, max_size=2**20, **options) as server:
        yield int(next(iter(server.sockets)).getsockname()[1])


def test_messages_both_ways_with_fragments_ping_and_the_signed_headers() -> None:
    seen: list[Any] = []

    async def handler(ws: ServerConnection) -> None:
        seen.append(ws.request.headers.get("Agent-Key") if ws.request is not None else None)
        seen.append(await ws.recv())
        seen.append(await ws.recv())
        pong = await ws.ping(b"p")
        await ws.send([b"ab", b"cd"])  # one message in two fragments
        await pong
        await ws.send("text" * 100)

    async def main() -> None:
        async with ws_server(handler) as port, connect(f"ws://127.0.0.1:{port}/v1/vnc/x?y=1", {"Agent-Key": "k"}) as ws:
            await ws.send(b"\x00\x01")
            await ws.send("paired?")
            assert await ws.recv() == b"abcd"
            assert await ws.recv() == "text" * 100
            with pytest.raises(SocketClosed):
                await ws.recv()  # the handler ended: the server closed
            with pytest.raises(SocketClosed):
                await ws.send(b"late")
            with pytest.raises(SocketClosed):
                await ws.recv()

    asyncio.run(main())
    assert seen[0] == "k"
    assert seen[1:] == [b"\x00\x01", "paired?"]


def test_tls_with_the_ca_of_ssl_cert_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ca = DoubleCA.create(tmp_path / "ca")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca.ca_file))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca.cert_file, ca.key_file)

    async def handler(ws: ServerConnection) -> None:
        await ws.send(await ws.recv())

    async def main() -> None:
        async with ws_server(handler, ssl=context) as port, connect(f"wss://localhost:{port}/t", {}) as ws:
            await ws.send(b"secure")
            assert await ws.recv() == b"secure"

    asyncio.run(main())


@asynccontextmanager
async def raw_server(answer: Callable[[str], bytes]) -> AsyncIterator[int]:
    """A server that reads the upgrade request and writes `answer(key)`, then closes."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n") if line.startswith("Sec-WebSocket-Key"))
        writer.write(answer(key))
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()


def upgraded(key: str, *frames: bytes) -> bytes:
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    head = f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    return head.encode() + b"".join(frames)


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (lambda key: b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n", 403),
        (lambda key: b"nonsense\r\n\r\n", 0),
        (lambda key: b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: wrong\r\n\r\n", 0),
        (lambda key: b"HTTP/1.1 101", 0),
    ],
)
def test_a_refused_or_foreign_handshake_fails(answer: Callable[[str], bytes], status: int) -> None:
    async def main() -> None:
        async with raw_server(answer) as port:
            with pytest.raises(HandshakeFailed) as failed:
                async with connect(f"ws://127.0.0.1:{port}/v1/vnc/x", {}):
                    pass
            assert failed.value.status == status

    asyncio.run(main())


def test_only_websocket_addresses_are_opened() -> None:
    async def main() -> None:
        with pytest.raises(ValueError, match="ws:// or wss://"):
            async with connect("http://127.0.0.1/x", {}):
                pass

    asyncio.run(main())


@pytest.mark.parametrize(
    "frames",
    [
        [b"\x82\x81abcdX"],  # a masked server frame
        [b"\x83\x00"],  # an unknown kind
        [b"\x80\x01x"],  # a continuation without a start
        [b"\x02\x01a", b"\x82\x01b"],  # a new message inside a fragmented one
        [b"\x82\x7f" + struct.pack(">Q", MAX_MESSAGE + 1)],  # too large (64-bit length)
        [b"\x02\x7e" + struct.pack(">H", 40000) + b"x" * 40000, b"\x80\x7e" + struct.pack(">H", 40000) + b"y" * 40000],
        [b"\x81\x02\xff\xfe"],  # a text that is no UTF-8
        [b"\x88\x02\x03\xe8"],  # a close
        [b"\x82"],  # the connection ends inside a frame
    ],
)
def test_frames_that_close_the_socket(frames: list[bytes]) -> None:
    async def main() -> None:
        async with (
            raw_server(lambda key: upgraded(key, *frames)) as port,
            connect(f"ws://127.0.0.1:{port}/x", {}) as ws,
        ):
            with pytest.raises(SocketClosed):
                await ws.recv()

    asyncio.run(main())


def test_a_pong_is_passed_over_and_a_medium_message_is_read() -> None:
    medium = b"m" * 300

    async def main() -> None:
        frames = (b"\x8a\x00", b"\x82\x7e" + struct.pack(">H", len(medium)) + medium)
        async with (
            raw_server(lambda key: upgraded(key, *frames)) as port,
            connect(f"ws://127.0.0.1:{port}/x", {}) as ws,
        ):
            assert await ws.recv() == medium

    asyncio.run(main())


class BrokenWriter:
    """A writer whose peer is gone."""

    def __init__(self, error: type[BaseException] = ConnectionResetError) -> None:
        self.error = error
        self.closed = False

    def write(self, data: bytes) -> None:
        raise self.error

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        raise ConnectionResetError


def test_a_gone_peer_closes_the_socket_and_close_is_quiet() -> None:
    async def main() -> None:
        socket = ClientSocket(asyncio.StreamReader(), BrokenWriter())  # type: ignore[arg-type]
        with pytest.raises(SocketClosed):
            await socket.send(b"x")
        assert socket.closed
        await socket.close()
        quiet = ClientSocket(asyncio.StreamReader(), BrokenWriter(RuntimeError))  # type: ignore[arg-type]
        await quiet.close()
        assert quiet.closed

    asyncio.run(main())


def test_frames_of_every_length_are_masked() -> None:
    for size, head in ((5, 2), (300, 4), (70000, 10)):
        built = frame(2, b"z" * size)
        assert built[0] == 0x82
        assert built[1] & 0x80
        mask = built[head : head + 4]
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(built[head + 4 :]))
        assert payload == b"z" * size
    assert accept_of("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="  # RFC 6455 section 1.3
    assert wsclient.TIMEOUT > 0
