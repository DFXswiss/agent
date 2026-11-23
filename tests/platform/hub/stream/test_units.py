"""U-HUB-stream (units): the per-stream state machine (held-back frames, overflow, filters, closing), the device
socket queue, the sender batches of the bus and the service's edges (not started, closed loop, missing event,
failing opening or heartbeat, an app without a live service)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from websockets.exceptions import InvalidStatus

from agent_hub.events.notify import Appended
from agent_hub.stream import bus as bus_module
from agent_hub.stream import presence, sse
from agent_hub.stream import service as service_module
from agent_hub.stream.bus import Bus
from agent_hub.stream.publish import Audience, MemoryPublisher, Published, current_publisher, set_publisher
from agent_hub.stream.service import StreamService, close_streams, service_of
from agent_hub.stream.sse import BrowserStream, LiveEvent, Opening, StreamQuery
from agent_hub.stream.ws import DeviceSocket
from tests.platform.hub.conftest import FIXED_NOW, HubHarness
from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import LiveHub, Refused, browser_key, wait_for

ORG = uuid.UUID("01a20000-0000-7000-8000-000000000001")
WG = uuid.UUID("01a20000-0000-7000-8000-000000000002")
USER = uuid.UUID("01a20000-0000-7000-8000-000000000003")
SESSION = uuid.UUID("01a20000-0000-7000-8000-000000000004")
EVENT = {"header": {}, "ct": "", "sig": ""}


def _stream(
    *, monitoring: bool = False, buffer: int = 10, watched: frozenset[uuid.UUID] = frozenset()
) -> BrowserStream:
    query = StreamQuery(ORG, watched, {}, monitoring)
    return BrowserStream(USER, query, frozenset({ORG, WG}), watched, buffer=buffer)


def _event(seq: int, *, scope: uuid.UUID = WG, kind: str = "session.output", org: uuid.UUID = ORG) -> LiveEvent:
    return LiveEvent(org, scope, seq, kind, SESSION, EVENT)


def _frames(stream: BrowserStream) -> list[str]:
    chunks = asyncio.run(stream.take(0.01))
    return [chunk.split(b"\n", 1)[0].decode() for chunk in chunks]


def test_frames_before_the_opening_are_held_back_and_filtered() -> None:
    stream = _stream()
    stream.offer_event(_event(3))
    stream.offer_event(_event(5))
    stream.offer(Published(Audience("user", USER), "wraps", {}))
    stream.prime(Opening([b"event: hello\ndata: {}\n\n"], {ORG: 0, WG: 4}))
    assert _frames(stream) == ["event: hello", "event: event", "event: wraps"]  # seq 3 was below the head
    assert stream.sent[WG] == 5


def test_held_back_overflow_and_resync_before_the_opening() -> None:
    stream = _stream(buffer=2)
    for seq in range(1, 4):
        stream.offer_event(_event(seq))
    stream.prime(Opening([], {WG: 0}))
    assert _frames(stream) == ["event: resync", "event: event"]
    early = _stream()
    early.resync("buffer_overflow")
    early.prime(Opening([], {}))
    assert _frames(early) == ["event: resync"]


def test_event_filters() -> None:
    stream = _stream(watched=frozenset({SESSION}))
    stream.prime(Opening([], {WG: 2}))
    stream.offer_event(_event(3, org=uuid.UUID(int=9)))  # another org
    stream.offer_event(_event(3, scope=uuid.UUID(int=8)))  # unreadable scope
    stream.offer_event(_event(2))  # already sent
    stream.offer_event(_event(3, kind="session.terminal"))
    other = LiveEvent(ORG, WG, 4, "session.terminal", uuid.UUID(int=7), EVENT)
    stream.offer_event(other)  # not watched
    stream.offer_event(LiveEvent(ORG, WG, 5, "session.terminal", None, EVENT))
    assert _frames(stream) == ["event: terminal"]


def test_published_filters_and_closing() -> None:
    stream = _stream()
    stream.prime(Opening([], {}))
    stream.offer(Published(Audience("org", ORG), "metrics", {"samples": []}))  # no monitoring flag
    stream.offer(Published(Audience("org", ORG), "task", {"wg_id": 7}))  # not a workgroup ID: org-wide
    stream.offer(Published(Audience("org", ORG), "operation", {"wg_id": str(uuid.UUID(int=8))}))  # unreadable
    stream.offer(Published(Audience("org", ORG), "notification", {"wg_id": str(uuid.UUID(int=8))}))
    assert _frames(stream) == ["event: notification"]  # a row without a workgroup reaches nobody
    stream.close()
    stream.close("hub_restart")  # already closed: nothing more
    stream.offer(Published(Audience("org", ORG), "log", {}))
    assert stream.finished
    assert _frames(stream) == []
    assert asyncio.run(_stream().take(0.01)) == []


def test_device_socket_queue() -> None:
    socket = DeviceSocket(USER, None, FIXED_NOW, buffer=2)  # type: ignore[arg-type]
    socket.close(1008)
    socket.close(1012)
    assert socket.close_code == 1008
    socket.offer("reconcile", {})
    assert not socket._hints


def test_receive_loop_stops_when_the_socket_is_closed_meanwhile() -> None:
    frame = {
        "type": "heartbeat",
        "capacity": dict.fromkeys(
            ("vm_memory_budget_mib", "vm_memory_used_mib", "vm_slots_total", "vm_slots_free", "pool_ready"), 0
        ),
        "sessions": [],
        "vms": [],
    }

    class Socket:
        async def receive(self) -> dict[str, Any]:
            return {"type": "websocket.receive", "text": json.dumps(frame)}

    device = DeviceSocket(USER, Socket(), FIXED_NOW)  # type: ignore[arg-type]

    async def on_heartbeat(_capacity: dict[str, int]) -> bool:
        device.close(1012)
        return True

    asyncio.run(device.receive_loop(1.0, on_heartbeat))
    assert device.close_code == 1012


def test_sender_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    class Connection:
        def execute(self, *_args: Any) -> None:
            sent.append(1)

    class Pool:
        def __init__(self) -> None:
            self.batches: list[int] = []

        @contextmanager
        def connection(self) -> Iterator[Connection]:
            before = len(sent)
            yield Connection()
            self.batches.append(len(sent) - before)

    pool = Pool()
    bus = Bus({}, pool, "me", on_event=print, on_message=print, on_gap=print)  # type: ignore[arg-type]
    for index in range(150):
        bus.send(str(index))
    bus._queue.put(None)
    bus._send_loop()
    assert pool.batches == [100, 50]
    bus.send("late")
    bus._queue.put(None)
    bus.send("after the end")
    bus._send_loop()
    assert pool.batches == [100, 50, 1, 1]  # everything queued before the stop is still sent
    assert bus_module.WAKE == ""


def test_memory_publisher_clear() -> None:
    fake = MemoryPublisher()
    previous = set_publisher(fake)
    try:
        assert current_publisher() is fake
        fake.publish(Published(Audience("org", ORG), "task", {}))
        fake.clear()
        assert fake.items() == []
    finally:
        set_publisher(previous)


def test_service_edges(hub: HubHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    service = StreamService(hub.app.state.hub)
    item = Published(Audience("org", ORG), "task", {})
    service.publish(item)  # not started: no loop, nothing delivered here
    closed = asyncio.new_event_loop()
    closed.close()
    service.loop = closed
    service.publish(item)  # the loop closed meanwhile: dropped
    service._orgs = frozenset({ORG})
    service._on_event(Appended(ORG, "org", ORG, 999))  # no such event
    service._remove(_stream())  # never registered
    monkeypatch.setattr(Bus, "wait_ready", lambda _self, timeout=0: False)

    async def cycle() -> None:
        await service.start()
        await service.stop()

    asyncio.run(cycle())
    assert "not listening yet" in caplog.text
    assert service_of(FastAPI()) is None
    asyncio.run(close_streams(FastAPI()))


def test_an_app_without_live_service(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    saved = live.app.state.stream
    del live.app.state.stream
    try:
        with pytest.raises(Refused) as refused:
            live.stream(browser_key(scene.reader), org=scene.org)
        assert (refused.value.status, refused.value.body) == (503, {"error": "unavailable"})
        with pytest.raises(InvalidStatus):
            live.device(hub.keys.device())
    finally:
        live.app.state.stream = saved


def test_a_failing_opening_unregisters_the_stream(scene: Scene, live: LiveHub, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(service_module, "opening", broken)
    with pytest.raises(Refused) as refused:
        live.stream(browser_key(scene.reader), org=scene.org)
    assert refused.value.status == 503
    assert live.service.by_org == {}


def test_a_failing_heartbeat_closes_the_socket_with_1011(
    hub: HubHarness, live: LiveHub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database down")

    client = live.device(hub.keys.device())
    assert client.hint()["type"] == "hello"
    monkeypatch.setattr(presence, "heartbeat", broken)
    client.heartbeat()
    assert client.close_code() == 1011
    assert wait_for(lambda: "device socket" in caplog.text)
    assert httpx.get(f"{live.base}/v1/health", timeout=5).status_code == 200


def test_the_byte_limit_keeps_a_pending_hello() -> None:
    stream = _stream()
    stream.byte_limit = 200
    stream.prime(Opening([b"event: hello\ndata: {}\n\n"], {}))
    stream.offer(Published(Audience("user", USER), "wraps", {"pad": "x" * 150}))
    stream.offer(Published(Audience("user", USER), "wraps", {"pad": "y" * 150}))
    assert _frames(stream) == ["event: hello", "event: resync", "event: wraps"]
    stream.offer(Published(Audience("user", USER), "wraps", {"pad": "x" * 150}))
    stream.offer(Published(Audience("user", USER), "wraps", {"pad": "y" * 150}))
    assert _frames(stream) == ["event: resync", "event: wraps"]  # no hello pending any more


def _notification(org: uuid.UUID, wg: object) -> Published:
    return Published(Audience("user", USER), "notification", {"org_id": str(org), "wg_id": wg})


def test_notifications_follow_the_org_and_workgroup_of_the_stream() -> None:
    stream = _stream()
    stream.prime(Opening([], {}))
    for item in (
        _notification(uuid.UUID(int=9), None),  # another org
        _notification(ORG, str(uuid.UUID(int=8))),  # an unreadable workgroup
        _notification(ORG, "not a uuid"),
        _notification(ORG, str(WG)),
        _notification(ORG, None),
    ):
        stream.offer(item)
    assert _frames(stream) == ["event: notification", "event: notification"]


def _sample(kind: str) -> dict[str, Any]:
    return {"entity_kind": kind, "entity_id": str(USER), "sample": {}}


def test_metrics_without_a_workgroup_carry_device_samples_only() -> None:
    stream = _stream(monitoring=True)
    stream.prime(Opening([], {}))
    org = Audience("org", ORG)
    stream.offer(Published(org, "metrics", {"samples": [_sample("device")]}))
    stream.offer(Published(org, "metrics", {"samples": [_sample("device"), _sample("vm")]}))
    stream.offer(Published(org, "metrics", {"samples": "nope"}))
    stream.offer(Published(org, "metrics", {"samples": [_sample("session")]}, wg_id=WG))
    stream.offer(Published(org, "session", {"session": "nope"}))
    assert _frames(stream) == ["event: metrics", "event: metrics"]


def test_resync_for_oversized_frames_of_another_hub() -> None:
    watching = _stream(monitoring=True)
    plain = _stream()
    for stream in (watching, plain):
        stream.prime(Opening([], {}))
        stream.resync_for(Published(Audience("org", ORG), "metrics", {}))
        stream.resync_for(Published(Audience("org", ORG), "task", {}, wg_id=uuid.UUID(int=8)))
        stream.resync_for(Published(Audience("org", ORG), "task", {}, wg_id=WG))
        stream.resync_for(Published(Audience("user", USER), "notification", {}))
    assert _frames(watching) == ["event: resync"] * 3
    assert _frames(plain) == ["event: resync"] * 2


def test_replay_past_the_total_byte_limit(scene: Scene, live: LiveHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sse, "REPLAY_TOTAL_BYTES", 1)
    rows = scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg)
    head = int(rows[0][0]) if rows else 0
    scene.post(scene.writer, scene.wg_event(scene.writer))
    stream = live.stream(browser_key(scene.reader), org=scene.org, cursors=f"{scene.wg}:{head}")
    assert [item.event for item in stream.frames(2)] == ["hello", "resync"]


def test_a_shutdown_during_the_opening_refuses_the_stream(
    scene: Scene, live: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = live.service._rights

    def closing_meanwhile(*args: Any) -> Any:
        live.service.closing = True
        return original(*args)

    monkeypatch.setattr(live.service, "_rights", closing_meanwhile)
    with pytest.raises(Refused) as refused:
        live.stream(browser_key(scene.reader), org=scene.org)
    assert refused.value.status == 503
    assert live.service.by_org == {}


def test_a_shutdown_during_the_connect_refuses_the_socket(
    hub: HubHarness, live: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = presence.connect

    def closing_meanwhile(*args: Any) -> Any:
        live.service.closing = True
        return original(*args)

    monkeypatch.setattr(presence, "connect", closing_meanwhile)
    key = hub.keys.device()
    with pytest.raises(InvalidStatus):
        live.device(key)

    def present() -> Any:
        with hub.pool.connection() as connection:
            query = "SELECT count(*) FROM hub.device_presence WHERE device_id = %s"
            return connection.execute(query, (key.principal.principal_id,)).fetchone()

    assert wait_for(lambda: present() == (0,))
    assert live.service.devices == {}


def test_a_device_that_became_unusable_after_the_gate_is_refused(
    hub: HubHarness, live: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(presence, "connect", lambda *_args: None)
    with pytest.raises(InvalidStatus):
        live.device(hub.keys.device())
    assert live.service.devices == {}
