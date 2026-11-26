"""U-HUB-vnc (relay): the pairing and copying of a VNC tunnel's two ends in memory (ADR-101 section 3) - one end per
side, `{"type":"paired"}` to both, binary records unchanged in both directions, the end reasons, the limits and the
audit rows without content."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any

from agent_hub.vnc import relay
from agent_hub.vnc.relay import CLIENT, DEVICE, PAIRED, RECORD_MAX, Tunnels

REQUEST = uuid.UUID("01930060-0000-7000-8000-000000000101")


class FakeEnd:
    """One WebSocket end: `incoming` is what its peer program sends, `sent` what the hub sent to it."""

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        self.sent: list[object] = []
        self.accepted = False
        self.closed: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> Mapping[str, Any]:
        return await self.incoming.get()

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        if self.closed is None:
            self.closed = code
        self.incoming.put_nowait({"type": "websocket.disconnect"})

    def push(self, data: bytes | str) -> None:
        key = "bytes" if isinstance(data, bytes) else "text"
        self.incoming.put_nowait({"type": "websocket.receive", key: data})


class Audits:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, action: str, detail: Mapping[str, int | str]) -> None:
        self.rows.append((action, dict(detail)))


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def _until(condition: Any) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


def test_both_ends_are_paired_and_records_pass_unchanged() -> None:
    async def main() -> None:
        clock, audits = Clock(), Audits()
        tunnels = Tunnels(clock=clock)
        client, device = FakeEnd(), FakeEnd()
        first = asyncio.ensure_future(tunnels.run(CLIENT, client, REQUEST, seconds=60, audit=audits))
        await _until(lambda: client.accepted)
        assert client.sent == []
        assert audits.rows == []
        second = asyncio.ensure_future(tunnels.run(DEVICE, device, REQUEST, seconds=60, audit=audits))
        await _until(lambda: client.sent == [PAIRED] and device.sent == [PAIRED])
        assert audits.rows == [("vnc.connect", {})]
        device.push(b"RFB 003.008\n")
        client.push(b"\x01" * RECORD_MAX)
        await _until(lambda: len(client.sent) == 2 and len(device.sent) == 2)
        assert client.sent[1] == b"RFB 003.008\n"
        assert device.sent[1] == b"\x01" * RECORD_MAX
        clock.now = 107.9
        client.incoming.put_nowait({"type": "websocket.disconnect"})
        await asyncio.gather(first, second)
        assert device.closed == 1000
        assert audits.rows[-1] == ("vnc.close", {"reason": "client_left", "seconds": 7, "bytes": 12 + RECORD_MAX})
        assert tunnels.pairs == {}

    asyncio.run(main())


def test_a_second_end_of_the_same_side_is_refused() -> None:
    async def main() -> None:
        tunnels, audits = Tunnels(pair_seconds=0.2), Audits()
        first, second = FakeEnd(), FakeEnd()
        waiting = asyncio.ensure_future(tunnels.run(CLIENT, first, REQUEST, seconds=60, audit=audits))
        await _until(lambda: first.accepted)
        await tunnels.run(CLIENT, second, REQUEST, seconds=60, audit=audits)
        assert (second.accepted, second.closed) == (False, relay.POLICY_VIOLATION)
        await waiting  # nobody joined: the end leaves after the pairing time
        assert first.sent == []
        assert first.closed == 1000
        assert audits.rows == []
        assert tunnels.pairs == {}

    asyncio.run(main())


def _broken(record: bytes | str, reason: str) -> None:
    async def main() -> None:
        tunnels, audits = Tunnels(), Audits()
        client, device = FakeEnd(), FakeEnd()
        ends = [
            asyncio.ensure_future(tunnels.run(CLIENT, client, REQUEST, seconds=60, audit=audits)),
            asyncio.ensure_future(tunnels.run(DEVICE, device, REQUEST, seconds=60, audit=audits)),
        ]
        await _until(lambda: device.sent == [PAIRED])
        device.push(record)
        await asyncio.gather(*ends)
        assert client.sent == [PAIRED]  # nothing of the broken record reached the client
        assert audits.rows[-1][1]["reason"] == reason

    asyncio.run(main())


def test_a_text_record_or_a_record_too_large_ends_the_tunnel() -> None:
    _broken("hello", "record_invalid")
    _broken(b"\x00" * (RECORD_MAX + 1), "record_too_large")


def test_the_tunnel_ends_at_the_requests_expiry() -> None:
    async def main() -> None:
        tunnels, audits = Tunnels(), Audits()
        client, device = FakeEnd(), FakeEnd()
        await asyncio.gather(
            tunnels.run(CLIENT, client, REQUEST, seconds=0.2, audit=audits),
            tunnels.run(DEVICE, device, REQUEST, seconds=0.2, audit=audits),
        )
        assert audits.rows[-1][1]["reason"] == "expired"

    asyncio.run(main())


def test_a_record_after_the_other_end_left_is_dropped() -> None:
    async def main() -> None:
        tunnels, _audits = Tunnels(), Audits()
        client, device = FakeEnd(), FakeEnd()
        pair = relay.Pair(REQUEST)
        tunnels.pairs[REQUEST] = pair
        pair.ends[DEVICE] = device
        pair.ready.set()
        device.push(b"late")
        pair.ends.pop(DEVICE)
        await tunnels._copy(DEVICE, device, pair)
        assert pair.reason == "client_left"
        assert client.sent == []

    asyncio.run(main())
