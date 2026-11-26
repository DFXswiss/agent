"""U-RUN-vnc (tunnel): the records of a VNC tunnel (ADR-101 section 3) - a key per direction, the record number as
nonce, forged, repeated, reordered and foreign records refused, and `splice` between a local stream and the socket
with its end reasons."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_runner.vnc import tunnel
from agent_runner.vnc.tunnel import (
    CHUNK,
    CLIENT,
    DEVICE,
    SocketClosed,
    Tunnel,
    TunnelBroken,
    close_writer,
    derive,
    new_key,
    splice,
)

REQUEST = "01930060-0000-7000-8000-000000000101"


def pair(key: bytes | None = None) -> tuple[Tunnel, Tunnel]:
    key = key or new_key()
    return Tunnel(key, REQUEST, CLIENT), Tunnel(key, REQUEST, DEVICE)


def test_each_direction_has_its_own_key_and_records_open_in_order_only() -> None:
    key = new_key()
    assert len(key) == tunnel.KEY_BYTES
    assert new_key() != key
    assert derive(key, REQUEST, b"c2d") != derive(key, REQUEST, b"d2c")
    assert derive(key, REQUEST, b"c2d") != derive(key, "01930060-0000-7000-8000-000000000102", b"c2d")
    with pytest.raises(ValueError, match="tunnel key length"):
        derive(b"short", REQUEST, b"c2d")
    client, device = pair(key)
    first, second = client.seal(b"RFB"), client.seal(b"")
    assert device.open(first) == b"RFB"
    assert device.open(second) == b""
    with pytest.raises(TunnelBroken) as repeated:
        device.open(first)  # a repeated record
    assert repeated.value.reason == "record_invalid"
    reply = device.seal(b"x" * CHUNK)
    with pytest.raises(TunnelBroken):
        device.open(reply)  # its own direction never opens
    assert client.open(reply) == b"x" * CHUNK
    with pytest.raises(ValueError, match="chunk too large"):
        client.seal(b"x" * (CHUNK + 1))


@pytest.mark.parametrize("record", ["text", b"short", b"x" * (tunnel.RECORD_MAX + 1), None])
def test_records_of_the_wrong_form_break_the_tunnel(record: Any) -> None:
    _client, device = pair()
    with pytest.raises(TunnelBroken):
        device.open(record)


def test_a_record_of_another_tunnel_or_out_of_order_breaks_it() -> None:
    client, device = pair()
    other, _ = pair()
    with pytest.raises(TunnelBroken):
        device.open(other.seal(b"foreign"))
    client.seal(b"skipped")
    with pytest.raises(TunnelBroken):
        device.open(client.seal(b"second"))  # the first record was dropped


class MemorySocket:
    """One end of an in-memory socket pair."""

    def __init__(self, inbox: asyncio.Queue[Any], outbox: asyncio.Queue[Any]) -> None:
        self.inbox = inbox
        self.outbox = outbox

    async def send(self, message: bytes) -> None:
        await self.outbox.put(message)

    async def recv(self) -> Any:
        message = await self.inbox.get()
        if isinstance(message, Exception):
            raise message
        return message


def sockets() -> tuple[MemorySocket, MemorySocket]:
    left: asyncio.Queue[Any] = asyncio.Queue()
    right: asyncio.Queue[Any] = asyncio.Queue()
    return MemorySocket(left, right), MemorySocket(right, left)


class Sink:
    """The local writing end; `fail` makes every write fail like a peer that is gone."""

    def __init__(self, fail: bool = False) -> None:
        self.data = b""
        self.fail = fail

    def write(self, data: bytes) -> None:
        if self.fail:
            raise ConnectionResetError
        self.data += data

    async def drain(self) -> None:
        return None


def feed(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def test_splice_copies_both_ways_until_one_side_ends() -> None:
    async def main() -> None:
        client_socket, device_socket = sockets()
        client, device = pair()
        client_sink, device_sink = Sink(), Sink()
        server_said = asyncio.StreamReader()
        server_said.feed_data(b"RFB 003.008\n")
        viewer = asyncio.ensure_future(splice(feed(b"RFB 003.008\n", eof=False), client_sink, client_socket, client))
        server = asyncio.ensure_future(splice(server_said, device_sink, device_socket, device, idle=5))
        for _ in range(200):
            if client_sink.data and device_sink.data:
                break
            await asyncio.sleep(0.01)
        assert client_sink.data == device_sink.data == b"RFB 003.008\n"
        server_said.feed_eof()  # the VNC server closed: its side ends, the viewer's side sees the peer close
        assert await server == "local_closed"
        assert await viewer == "peer_closed"

    asyncio.run(main())


def test_splice_ends_when_nothing_comes_in_for_the_idle_time() -> None:
    async def main() -> None:
        _client_socket, device_socket = sockets()
        _client, device = pair()
        assert await splice(feed(eof=False), Sink(), device_socket, device, idle=0.05) == "idle"

    asyncio.run(main())


def test_splice_ends_on_a_closed_socket_a_gone_local_peer_and_a_broken_record() -> None:
    async def main() -> None:
        client_socket, device_socket = sockets()
        client, device = pair()
        await client_socket.inbox.put(SocketClosed())
        assert await splice(feed(eof=False), Sink(), client_socket, client) == "socket_closed"
        await device_socket.send(device.seal(b"data"))  # lands in the client's inbox
        assert await splice(feed(eof=False), Sink(fail=True), client_socket, client) == "local_closed"
        await client_socket.inbox.put(b"forged-record-of-the-hub")
        with pytest.raises(TunnelBroken):
            await splice(feed(eof=False), Sink(), client_socket, client)

    asyncio.run(main())


def test_a_local_read_error_ends_the_local_side() -> None:
    class Failing(asyncio.StreamReader):
        async def read(self, n: int = -1) -> bytes:
            raise ConnectionResetError

    async def main() -> None:
        client_socket, device_socket = sockets()
        client, device = pair()
        assert await splice(Failing(), Sink(), client_socket, client) == "local_closed"
        assert device.open(await device_socket.recv()) == b""

    asyncio.run(main())


def test_close_writer_ignores_a_peer_that_is_gone() -> None:
    class Gone:
        def close(self) -> None:
            raise ConnectionResetError

        async def wait_closed(self) -> None:
            return None

    async def main() -> None:
        await close_writer(Gone())  # type: ignore[arg-type]

    asyncio.run(main())
