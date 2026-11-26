"""The encrypted VNC tunnel between the owner's client and the device (ADR-101 section 3).

The device draws a fresh tunnel key (`KEY_BYTES`) for every `vnc.open` and sends it to the requester inside the sealed
response (`step: "vnc"`); the hub relays records it cannot open and never sees the screen, the keys typed or the
one-time password. Each direction has its own key, `HKDF-SHA256(key, salt = the request ID's 16 bytes,
info = "agent-vnc-v1\\0" + direction)` with the direction `c2d` (client to device) or `d2c`. A record is one
WebSocket binary message `AES-256-GCM(k_direction, nonce, chunk)`; the nonce is the record's number in its direction
(12 bytes big endian, from 0, never sent), so a record the hub drops, repeats or reorders breaks the tunnel
(`TunnelBroken`). A chunk holds 1 to `CHUNK` bytes of the RFB stream; an empty chunk ends its direction (that side
closed its local connection).

`splice` joins a local byte stream (the VNC server in the VM on the device, the VNC viewer on the client) with the
socket until one side ends; on the device it also ends after `idle` seconds without a record from the client (the
VNC viewer sends nothing while nobody works on a screen that does not change).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_BYTES = 32
CHUNK = 16 * 1024
TAG_BYTES = 16
RECORD_MAX = CHUNK + TAG_BYTES
INFO = b"agent-vnc-v1\x00"
CLIENT, DEVICE = "client", "device"
_SENDS = {CLIENT: b"c2d", DEVICE: b"d2c"}
_RECEIVES = {CLIENT: b"d2c", DEVICE: b"c2d"}


class SocketClosed(Exception):
    """The tunnel's WebSocket closed (raised by the socket of either end; the agent tool reaches no `websockets`)."""


class TunnelBroken(Exception):
    """A record that does not open (forged, dropped, repeated or reordered by the hub) or is not binary."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def new_key() -> bytes:
    """A fresh tunnel key."""
    return os.urandom(KEY_BYTES)


def derive(key: bytes, request_id: str, direction: bytes) -> bytes:
    """The key of one direction (module docstring)."""
    if len(key) != KEY_BYTES:
        raise ValueError("tunnel key length")
    salt = uuid.UUID(request_id).bytes
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=INFO + direction).derive(key)


def _nonce(number: int) -> bytes:
    return number.to_bytes(12, "big")


class Tunnel:
    """The records of one side of a tunnel: `seal` what it sends, `open` what it receives, each in order."""

    def __init__(self, key: bytes, request_id: str, side: str) -> None:
        self._send = AESGCM(derive(key, request_id, _SENDS[side]))
        self._receive = AESGCM(derive(key, request_id, _RECEIVES[side]))
        self.sent = 0
        self.received = 0

    def seal(self, chunk: bytes) -> bytes:
        if len(chunk) > CHUNK:
            raise ValueError("chunk too large")
        record = self._send.encrypt(_nonce(self.sent), chunk, None)
        self.sent += 1
        return record

    def open(self, record: object) -> bytes:
        if not isinstance(record, bytes) or not TAG_BYTES <= len(record) <= RECORD_MAX:
            raise TunnelBroken("record_invalid")
        try:
            chunk = self._receive.decrypt(_nonce(self.received), record, None)
        except InvalidTag:
            raise TunnelBroken("record_invalid") from None
        self.received += 1
        return chunk


class Socket(Protocol):
    """The tunnel's WebSocket: `send` a binary record, `recv` the next message; both raise `SocketClosed` once it
    closed."""

    def send(self, message: bytes) -> Awaitable[None]: ...

    def recv(self) -> Awaitable[bytes | str]: ...


Connect = Callable[[str, Mapping[str, str]], AbstractAsyncContextManager[Any]]
"""Opens the tunnel's WebSocket at a URL with the signed upgrade headers (the runner: `websockets`; the agent tool:
`agent_platform.wsclient`)."""


class Writer(Protocol):
    """The local side's writing end (`asyncio.StreamWriter`)."""

    def write(self, data: bytes) -> object: ...

    def drain(self) -> Awaitable[None]: ...


LOCAL_ERRORS = (OSError, asyncio.IncompleteReadError)


async def _outbound(reader: asyncio.StreamReader, socket: Socket, tunnel: Tunnel) -> str:
    while True:
        try:
            chunk = await reader.read(CHUNK)
        except LOCAL_ERRORS:
            chunk = b""
        await socket.send(tunnel.seal(chunk))
        if not chunk:
            return "local_closed"


async def _inbound(writer: Writer, socket: Socket, tunnel: Tunnel, idle: float | None) -> str:
    while True:
        try:
            message = await (socket.recv() if idle is None else asyncio.wait_for(socket.recv(), idle))
        except TimeoutError:
            return "idle"
        chunk = tunnel.open(message)
        if not chunk:
            return "peer_closed"
        try:
            writer.write(chunk)
            await writer.drain()
        except LOCAL_ERRORS:
            return "local_closed"


async def splice(
    reader: asyncio.StreamReader, writer: Writer, socket: Socket, tunnel: Tunnel, *, idle: float | None = None
) -> str:
    """Copy both ways until one side ends; why it ended: `local_closed`, `peer_closed`, `idle` or `socket_closed`
    (`TunnelBroken` for a record that does not open)."""
    tasks = [
        asyncio.ensure_future(_outbound(reader, socket, tunnel)),
        asyncio.ensure_future(_inbound(writer, socket, tunnel, idle)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    first = next(task for task in tasks if task in done)
    try:
        return first.result()
    except SocketClosed:
        return "socket_closed"


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a local writer, ignoring a peer that is gone already."""
    with contextlib.suppress(*LOCAL_ERRORS):
        writer.close()
        await writer.wait_closed()
