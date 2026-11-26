"""The relay of VNC tunnels (ADR-101 section 3).

A tunnel belongs to one active `vnc.open` device request: its two ends are the WebSocket of the owner's client
(`GET /v1/vnc/{request}`) and the WebSocket of the request's device (`GET /v1/device/vnc/{request}`). `Tunnels.run`
holds one end until the other one joins (at most `PAIR_SECONDS`; an end that leaves or sends before is let go at once),
then tells both ends `{"type":"paired"}` (the waiting end first, before any record is copied) and copies
the binary records of each end to the other unchanged until one end leaves, a record breaks the form (text, or more
than `RECORD_MAX` bytes) or the request's expiry is reached; then both ends are closed. The records are encrypted by
the two ends (`agent_runner.vnc.tunnel`): the hub never sees the screen, the keys typed or the one-time password.

One end per side and request: a second end of the same side is closed at once (policy violation). The pairing is in
the memory of the hub process (the hub runs as one process, runbooks/hub-operation.md).

Audit (metadata only): `vnc.connect` when both ends are joined, `vnc.close {reason, seconds, bytes}` when the tunnel
ends, both with the user as actor, the device and the request as target.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

PAIR_SECONDS = 120.0
RECORD_MAX = 16 * 1024 + 16
PAIRED = '{"type":"paired"}'
CLIENT, DEVICE = "client", "device"
OTHER = {CLIENT: DEVICE, DEVICE: CLIENT}
POLICY_VIOLATION = 1008
TOO_LARGE = 1009
REASONS = frozenset({"client_left", "device_left", "record_invalid", "record_too_large", "expired"})


class End(Protocol):
    """One end of a tunnel (a Starlette `WebSocket`)."""

    async def accept(self) -> None: ...

    async def receive(self) -> Mapping[str, Any]: ...

    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


Audit = Callable[[str, Mapping[str, int | str]], Awaitable[None]]


@dataclass(eq=False)
class Pair:
    request_id: UUID
    ends: dict[str, End] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    opened: float | None = None
    bytes: int = 0
    reason: str | None = None


class Tunnels:
    """The tunnels of this hub process."""

    def __init__(self, *, pair_seconds: float = PAIR_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.pair_seconds = pair_seconds
        self.clock = clock
        self.pairs: dict[UUID, Pair] = {}

    async def run(self, side: str, end: End, request_id: UUID, *, seconds: float, audit: Audit) -> None:
        """Hold `end` as the `side` of the request's tunnel for at most `seconds` (module docstring)."""
        pair = self.pairs.setdefault(request_id, Pair(request_id))
        if side in pair.ends:
            await end.close(POLICY_VIOLATION)
            return
        await end.accept()
        pair.ends[side] = end
        try:
            if len(pair.ends) == 2:
                # both ends learn of the pairing before any record is copied (the waiting end first)
                await pair.ends[OTHER[side]].send_text(PAIRED)
                await end.send_text(PAIRED)
                pair.opened = self.clock()
                pair.ready.set()
                await audit("vnc.connect", {})
            await self._serve(side, end, pair, seconds)
        finally:
            pair.ends.pop(side, None)
            with contextlib.suppress(Exception):
                await end.close()
            other = pair.ends.get(OTHER[side])
            if other is not None:
                with contextlib.suppress(Exception):
                    await other.close()
            if not pair.ends and self.pairs.get(request_id) is pair:
                del self.pairs[request_id]
                if pair.opened is not None:
                    seconds_open = int(self.clock() - pair.opened)
                    reason = pair.reason or f"{side}_left"
                    detail: dict[str, int | str] = {"reason": reason, "seconds": seconds_open, "bytes": pair.bytes}
                    await audit("vnc.close", detail)

    async def _serve(self, side: str, end: End, pair: Pair, seconds: float) -> None:
        started = self.clock()
        if not pair.ready.is_set() and not await self._paired(end, pair, min(self.pair_seconds, seconds)):
            return
        left = seconds - (self.clock() - started)
        try:
            await asyncio.wait_for(self._copy(side, end, pair), max(left, 0.0))
        except TimeoutError:
            pair.reason = pair.reason or "expired"

    @staticmethod
    async def _paired(end: End, pair: Pair, seconds: float) -> bool:
        """Wait for the other end; False when the time runs out or this end leaves (or sends) before it joined, so a
        gone end never holds its side."""
        ready = asyncio.ensure_future(pair.ready.wait())
        leaving = asyncio.ensure_future(end.receive())
        try:
            done, _ = await asyncio.wait((ready, leaving), timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (ready, leaving):
                waiter.cancel()
            await asyncio.gather(ready, leaving, return_exceptions=True)
        return ready in done and leaving not in done

    async def _copy(self, side: str, end: End, pair: Pair) -> None:
        while True:
            message = await end.receive()
            data = message.get("bytes")
            if message.get("type") == "websocket.disconnect":
                pair.reason = pair.reason or f"{side}_left"
                return
            if not isinstance(data, bytes):
                pair.reason = pair.reason or "record_invalid"
                return
            if len(data) > RECORD_MAX:
                pair.reason = pair.reason or "record_too_large"
                return
            other = pair.ends.get(OTHER[side])
            if other is None:
                pair.reason = pair.reason or f"{OTHER[side]}_left"
                return
            pair.bytes += len(data)
            await other.send_bytes(data)


TUNNELS = Tunnels()
