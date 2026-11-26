"""VNC double (ADR-101 section 7): a minimal RFB 3.8 server with classic VNC authentication, a viewer that
authenticates and clicks, and a screen (`agent_runner.vnc.screen.Screen`) whose VNC server is the double.

- `VncServer` serves one framebuffer of `width` x `height` on 127.0.0.1: version `RFB 003.008`, the single security
  type 2 (VNC authentication: a 16-byte challenge encrypted with DES under the bit-reversed password, as RFC 6143
  section 7.2.2), then ClientInit/ServerInit and the client messages SetPixelFormat, SetEncodings,
  FramebufferUpdateRequest (answered with one raw 1x1 rectangle), KeyEvent, PointerEvent and ClientCutText. A press of
  button 1 is a click: it is counted in `clicks` and handed to `on_click(x, y)` (the integration tests click the real
  page of the sandbox there). A wrong or missing password is refused (`refused`) with the RFB failure reason; an
  unknown message ends the connection.
- `view_and_click` is the person at the viewer: it authenticates with the password, reads the screen's size and one
  update, and clicks once at `(x, y)`; `{"refused": true}` when the server refuses the password.
- `DoubleScreen` is a screen whose `start(password)` sets the server's one-time password and whose `connect` opens a
  connection to the server, like `workvm-vnc` does in a VM.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
from collections.abc import Awaitable, Callable
from typing import Any

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from agent_runner.vnc.screen import Local, ScreenError

VERSION = b"RFB 003.008\n"
VNC_AUTH = 2
PIXEL_FORMAT = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
NAME = b"agent-vnc-double"
REFUSED = b"password refused"
CLIENT_MESSAGE_SIZES = {0: 19, 4: 7, 5: 5}


def vnc_key(password: str) -> bytes:
    """The DES key of a VNC password: the first 8 bytes, padded with zero bytes, each with its bits reversed."""
    raw = password.encode("latin-1")[:8].ljust(8, b"\0")
    return bytes(int(f"{byte:08b}"[::-1], 2) for byte in raw)


def vnc_response(password: str, challenge: bytes) -> bytes:
    """The answer to a VNC authentication challenge."""
    encryptor = Cipher(TripleDES(vnc_key(password) * 3), modes.ECB()).encryptor()  # DES of the RFB protocol
    return encryptor.update(challenge) + encryptor.finalize()


class VncServer:
    def __init__(
        self,
        *,
        width: int = 1280,
        height: int = 800,
        on_click: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.on_click = on_click
        self.password: str | None = None
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []
        self.sessions = 0
        self.refused = 0
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if await self._handshake(reader, writer):
                self.sessions += 1
                await self._messages(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        writer.write(VERSION)
        if await reader.readexactly(12) != VERSION:
            return False
        writer.write(bytes([1, VNC_AUTH]))
        if (await reader.readexactly(1))[0] != VNC_AUTH:
            return False
        challenge = os.urandom(16)
        writer.write(challenge)
        answer = await reader.readexactly(16)
        if self.password is None or answer != vnc_response(self.password, challenge):
            self.refused += 1
            writer.write(struct.pack(">II", 1, len(REFUSED)) + REFUSED)
            await writer.drain()
            return False
        writer.write(struct.pack(">I", 0))
        await reader.readexactly(1)  # ClientInit (shared flag)
        writer.write(struct.pack(">HH", self.width, self.height) + PIXEL_FORMAT + struct.pack(">I", len(NAME)) + NAME)
        await writer.drain()
        return True

    async def _messages(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pressed = False
        while True:
            kind = (await reader.readexactly(1))[0]
            if kind in CLIENT_MESSAGE_SIZES:
                data = await reader.readexactly(CLIENT_MESSAGE_SIZES[kind])
                if kind == 4:
                    down, keysym = struct.unpack(">BxxI", data)
                    if down:
                        self.keys.append(keysym)
                if kind == 5:
                    mask, x, y = struct.unpack(">BHH", data)
                    if mask & 1 and not pressed:
                        self.clicks.append((x, y))
                        if self.on_click is not None:
                            await self.on_click(x, y)
                    pressed = bool(mask & 1)
            elif kind == 2:
                count = struct.unpack(">xH", await reader.readexactly(3))[0]
                await reader.readexactly(4 * count)
            elif kind == 3:
                await reader.readexactly(9)
                rectangle = struct.pack(">HHHHi", 0, 0, 1, 1, 0)
                writer.write(struct.pack(">BxH", 0, 1) + rectangle + b"\0\0\0\0")
                await writer.drain()
            elif kind == 6:
                length = struct.unpack(">xxxI", await reader.readexactly(7))[0]
                await reader.readexactly(length)
            else:
                return


async def view_and_click(
    port: int, password: str, x: int, y: int, *, key: int | None = None, stay: bool = False
) -> dict[str, Any]:
    """The person at the viewer (module docstring); with `key` also one key press before the click, with `stay` the
    window stays open until the other side closes the connection."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        version = await reader.readexactly(12)
        writer.write(version)
        count = (await reader.readexactly(1))[0]
        types = await reader.readexactly(count)
        if VNC_AUTH not in types:
            return {"refused": True}
        writer.write(bytes([VNC_AUTH]))
        writer.write(vnc_response(password, await reader.readexactly(16)))
        if struct.unpack(">I", await reader.readexactly(4))[0] != 0:
            return {"refused": True}
        writer.write(b"\x01")
        width, height = struct.unpack(">HH", await reader.readexactly(4))
        await reader.readexactly(16)
        name = await reader.readexactly(struct.unpack(">I", await reader.readexactly(4))[0])
        writer.write(struct.pack(">BxH", 2, 1) + struct.pack(">i", 0))
        writer.write(struct.pack(">BBHHHH", 3, 1, 0, 0, width, height))
        await reader.readexactly(4 + 12 + 4)
        if key is not None:
            writer.write(struct.pack(">BBxxI", 4, 1, key) + struct.pack(">BBxxI", 4, 0, key))
        writer.write(struct.pack(">BBHH", 5, 1, x, y) + struct.pack(">BBHH", 5, 0, x, y))
        await writer.drain()
        while stay and await reader.read(65536):
            pass
        return {"width": width, "height": height, "name": name.decode()}
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


class DoubleScreen:
    """A screen whose VNC server is `server` (module docstring); `starts` and `stops` count the calls."""

    def __init__(self, server: VncServer, *, fail: bool = False) -> None:
        self.server = server
        self.fail = fail
        self.starts = 0
        self.stops = 0

    async def start(self, password: str) -> None:
        if self.fail:
            raise ScreenError("vnc_start_failed")
        self.starts += 1
        self.server.password = password

    async def connect(self) -> Local:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        return Local(reader, writer, None)

    async def stop(self) -> None:
        self.stops += 1
        self.server.password = None
