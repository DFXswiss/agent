"""The WebSocket client of the agent tool for the VNC tunnel (ADR-101 section 3; RFC 6455, standard library only: the
tool reaches no third-party package beyond `httpx` and `cryptography`).

`connect(url, headers)` opens `ws://` or `wss://` (TLS with the system's trust store, also `SSL_CERT_FILE`), sends the
upgrade request with the given signed headers and checks the answer: status 101 and the `Sec-WebSocket-Accept` of its
key, else `HandshakeFailed`. No redirect is followed and no proxy is used. The socket sends one unfragmented, masked
message per `send` (binary for bytes, text for a string) and `recv` returns the next complete message (fragments
joined, at most `MAX_MESSAGE` bytes); a ping is answered with a pong, a pong is passed over. A close, a message that is
too large, a frame of an unknown kind or a connection that ends make `recv` and `send` raise `SocketClosed`. Leaving
the context sends a close frame and closes the connection.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import ssl
import struct
from collections.abc import AsyncIterator, Mapping
from urllib.parse import SplitResult, urlsplit

from agent_runner.vnc.tunnel import SocketClosed

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TIMEOUT = 10.0
HEAD_BYTES = 16 * 1024
MAX_MESSAGE = 64 * 1024
TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 1, 2, 8, 9, 10, 0


class HandshakeFailed(Exception):
    """The hub did not accept the upgrade (`status`, or 0 for an answer that is no WebSocket upgrade)."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def accept_of(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def frame(opcode: int, payload: bytes) -> bytes:
    """One final, masked client frame."""
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        head = struct.pack(">BB", 0x80 | opcode, 0x80 | size)
    elif size < 65536:
        head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, size)
    else:
        head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, size)
    keystream = (mask * (size // 4 + 1))[:size]
    masked = (int.from_bytes(payload, "big") ^ int.from_bytes(keystream, "big")).to_bytes(size, "big")
    return head + mask + masked


class ClientSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.closed = False

    async def handshake(self, parts: SplitResult, headers: Mapping[str, str]) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {parts.netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *(f"{name}: {value}" for name, value in headers.items()),
        ]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        try:
            head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            raise HandshakeFailed(0) from None
        status_line, *fields = head.decode("latin-1").split("\r\n")
        parts_of_status = status_line.split(" ", 2)
        status = int(parts_of_status[1]) if len(parts_of_status) > 1 and parts_of_status[1].isdigit() else 0
        if status != 101:
            raise HandshakeFailed(status)
        answer = {name.strip().lower(): value.strip() for name, _, value in (f.partition(":") for f in fields if f)}
        if answer.get("sec-websocket-accept") != accept_of(key):
            raise HandshakeFailed(0)

    async def send(self, message: bytes | str) -> None:
        if self.closed:
            raise SocketClosed
        data, opcode = (message, BINARY) if isinstance(message, bytes) else (message.encode("utf-8"), TEXT)
        await self._write(frame(opcode, data))

    async def _write(self, data: bytes) -> None:
        try:
            self.writer.write(data)
            await self.writer.drain()
        except ConnectionError:
            self.closed = True
            raise SocketClosed from None

    async def _frame(self) -> tuple[bool, int, bytes]:
        first, second = await self.reader.readexactly(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", await self.reader.readexactly(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", await self.reader.readexactly(8))[0]
        if second & 0x80 or size > MAX_MESSAGE:  # a server never masks; nothing larger than a message is taken
            raise SocketClosed
        return bool(first & 0x80), first & 0x0F, await self.reader.readexactly(size)

    async def recv(self) -> bytes | str:
        if self.closed:
            raise SocketClosed
        parts: list[bytes] = []
        kind = None
        try:
            while True:
                final, opcode, payload = await self._frame()
                if opcode == PING:
                    await self._write(frame(PONG, payload))
                    continue
                if opcode == PONG:
                    continue
                if opcode not in (TEXT, BINARY, CONTINUATION) or (opcode == CONTINUATION) != bool(parts):
                    raise SocketClosed
                kind = kind or opcode
                parts.append(payload)
                if sum(len(part) for part in parts) > MAX_MESSAGE:
                    raise SocketClosed
                if final:
                    data = b"".join(parts)
                    return data if kind == BINARY else data.decode("utf-8")
        except (asyncio.IncompleteReadError, ConnectionError, UnicodeDecodeError, SocketClosed):
            await self.close()
            raise SocketClosed from None

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            with contextlib.suppress(ConnectionError, RuntimeError):
                self.writer.write(frame(CLOSE, struct.pack(">H", 1000)))
                await self.writer.drain()
        self.writer.close()
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            await self.writer.wait_closed()


@contextlib.asynccontextmanager
async def connect(url: str, headers: Mapping[str, str]) -> AsyncIterator[ClientSocket]:
    """The open WebSocket of `url` (module docstring)."""
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ValueError("a ws:// or wss:// URL")
    port = parts.port or (443 if secure else 80)
    context = ssl.create_default_context() if secure else None
    opening = asyncio.open_connection(
        parts.hostname, port, ssl=context, server_hostname=parts.hostname if secure else None, limit=HEAD_BYTES
    )
    reader, writer = await asyncio.wait_for(opening, TIMEOUT)
    socket = ClientSocket(reader, writer)
    try:
        await socket.handshake(parts, headers)
        yield socket
    finally:
        await socket.close()
