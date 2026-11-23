"""U-HUB-stream (bus across hubs): two hub apps with their own live services and bus connections on one database -
events, frames and hints published at one hub reach browsers and devices connected to the other, oversized frames
become resync/reconcile there, and a lost bus connection resyncs every receiver of that hub."""

from __future__ import annotations

import uuid

import psycopg

from agent_hub.stream.publish import Audience, Published
from tests.platform.hub.conftest import HubHarness, admin_params
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.stream.support import LiveHub, browser_key, free_port, live_hub, wait_for

HELLO_TYPE = "hello"


def _sample(device: uuid.UUID) -> dict[str, object]:
    sample = {
        "ts": "2026-11-21T12:00:00.123Z",
        "cpu_ratio": 0.25,
        "mem_used_bytes": 1 << 30,
        "mem_limit_bytes": 1 << 34,
        "disk_used_bytes": 1 << 35,
        "disk_limit_bytes": 1 << 40,
        "net_rx_bytes": 123456789,
        "net_tx_bytes": 987654321,
        "uptime_seconds": 3600,
        "reachable": True,
        "state": "running",
        "free_vm_slots": 3,
        "free_mem_bytes": 1 << 33,
    }
    return {"entity_kind": "device", "entity_id": str(device), "sample": sample}


def test_two_hubs_share_events_frames_and_hints(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    with live_hub(hub, free_port(15)) as other:
        here = live.stream(browser_key(scene.reader), org=scene.org, monitoring="1")
        there = other.stream(browser_key(scene.reader), org=scene.org, monitoring="1")
        assert here.frame().event == "hello"  # type: ignore[union-attr]
        assert there.frame().event == "hello"  # type: ignore[union-attr]
        device = hub.keys.device()
        device_id = device.principal.principal_id
        assert device_id is not None
        socket = other.device(device)
        assert socket.hint()["type"] == HELLO_TYPE
        # an event stored anywhere reaches both hubs through hub_events
        response = scene.post(scene.writer, scene.wg_event(scene.writer))
        seq = response.json()["results"][0]["scope_seq"]
        assert here.frame().data["scope_seq"] == seq  # type: ignore[union-attr]
        assert there.frame().data["scope_seq"] == seq  # type: ignore[union-attr]
        # a frame published at one hub: delivered there at once and at the other through hub_stream
        org = Audience("org", uuid.UUID(scene.org))
        live.service.publish(Published(org, "metrics", {"samples": []}))
        assert here.frame().event == "metrics"  # type: ignore[union-attr]
        assert there.frame().event == "metrics"  # type: ignore[union-attr]
        session = new_id()
        live.service.publish(Published(Audience("device", device_id), "session.start", {"session_id": session}))
        assert socket.hint() == {"type": "session.start", "session_id": session}
        # oversized: the publishing hub delivers the frame, the other one a resync / reconcile
        big = {"samples": [_sample(device_id) for _ in range(60)]}
        live.service.publish(Published(org, "metrics", big))
        assert here.frame().data == big  # type: ignore[union-attr]
        assert there.frame().data == {"reason": "buffer_overflow"}  # type: ignore[union-attr]
        live.service.publish(Published(Audience("device", device_id), "reconcile", {"pad": "x" * 9000}))
        assert socket.hint() == {"type": "reconcile"}
        live.service.publish(Published(Audience("device", uuid.UUID(new_id())), "reconcile", {"pad": "x" * 9000}))
        # the frames of events are never sent over hub_stream (they come from hub_events)
        live.service.publish(Published(org, "event", {}))
        assert here.quiet() == []
        assert there.quiet() == []


def test_a_lost_bus_connection_resyncs_every_receiver(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    stream = live.stream(browser_key(scene.reader), org=scene.org)
    assert stream.frame().event == "hello"  # type: ignore[union-attr]
    device = live.device(hub.keys.device())
    assert device.hint()["type"] == HELLO_TYPE
    first = live.service.bus.listener_pid
    with psycopg.connect(**admin_params(), autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (first,))
    assert stream.frame(10).data == {"reason": "buffer_overflow"}  # type: ignore[union-attr]
    assert device.hint(10) == {"type": "reconcile"}
    assert wait_for(lambda: live.service.bus.listener_pid not in (None, first))
