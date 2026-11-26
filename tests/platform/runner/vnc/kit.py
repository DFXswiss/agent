"""Test kit of the VNC suites (ADR-101): in-memory WebSockets of both tunnel ends joined by the hub's real relay
(`agent_hub.vnc.relay.Tunnels`), and a fake flow for the `vnc.open` handler."""

from __future__ import annotations

import asyncio
import contextlib
import io
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from agent_hub.vnc.relay import Tunnels
from agent_runner.vnc.tunnel import Connect, SocketClosed

CLOSED = object()


class ProgramSocket:
    """The socket a program (runner or agent tool) holds."""

    def __init__(self, inbox: asyncio.Queue[Any], hub: asyncio.Queue[Mapping[str, Any]]) -> None:
        self.inbox = inbox
        self.hub = hub

    async def send(self, message: bytes | str) -> None:
        key = "bytes" if isinstance(message, bytes) else "text"
        await self.hub.put({"type": "websocket.receive", key: message})

    async def recv(self) -> Any:
        message = await self.inbox.get()
        if message is CLOSED:
            self.inbox.put_nowait(CLOSED)
            raise SocketClosed
        return message


class HubEnd:
    """The same socket as the hub's relay sees it (a Starlette `WebSocket`)."""

    def __init__(self, inbox: asyncio.Queue[Mapping[str, Any]], program: asyncio.Queue[Any]) -> None:
        self.inbox = inbox
        self.program = program

    async def accept(self) -> None:
        return None

    async def receive(self) -> Mapping[str, Any]:
        return await self.inbox.get()

    async def send_text(self, data: str) -> None:
        await self.program.put(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.program.put(data)

    async def close(self, code: int = 1000) -> None:
        self.program.put_nowait(CLOSED)
        self.inbox.put_nowait({"type": "websocket.disconnect"})


@dataclass
class Wire:
    """Both ends of every tunnel, relayed by one `Tunnels` (the hub process) with recorded audit rows."""

    tunnels: Tunnels = field(default_factory=Tunnels)
    audits: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    urls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    async def audit(self, action: str, detail: Mapping[str, int | str]) -> None:
        self.audits.append((action, dict(detail)))

    def connector(self, side: str) -> Connect:
        @contextlib.asynccontextmanager
        async def opened(url: str, headers: Mapping[str, str]) -> AsyncIterator[ProgramSocket]:
            self.urls.append((url, dict(headers)))
            request = uuid.UUID(url.rsplit("/", 1)[1])
            program: asyncio.Queue[Any] = asyncio.Queue()
            hub: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
            task = asyncio.ensure_future(
                self.tunnels.run(side, HubEnd(hub, program), request, seconds=600, audit=self.audit)
            )
            try:
                yield ProgramSocket(program, hub)
            finally:
                hub.put_nowait({"type": "websocket.disconnect"})
                await task

        return opened


class Signer:
    def sign(self, method: str, target: str, body: bytes) -> Any:
        return SimpleNamespace(headers={"Agent-Key": "device.test", "Agent-Target": f"{method} {target}"})


@dataclass
class FakeFlow:
    """The view of a `vnc.open` flow the handler uses."""

    body: dict[str, Any]
    requester: str = "alice"
    user: str = "alice"
    id: str = "01930060-0000-7000-8000-000000000201"
    seconds: float = 600.0
    options: Any = None
    responses: list[tuple[dict[str, Any], bool]] = field(default_factory=list)
    context: Any = None
    service: Any = None

    def __post_init__(self) -> None:
        self.context = SimpleNamespace(log=io.StringIO(), vnc_options=self.options)
        client = SimpleNamespace(signer=Signer(), origin=SimpleNamespace(ws_base_url="ws://hub.test"))
        self.service = SimpleNamespace(sync=SimpleNamespace(client=client))

    def remaining(self) -> float:
        return self.seconds

    async def respond(self, body: Mapping[str, object], *, final: bool = False, to: Any = None) -> None:
        self.responses.append((dict(body), final))

    def log(self) -> str:
        text: str = self.context.log.getvalue()
        return text
