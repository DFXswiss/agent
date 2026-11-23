"""U-HUB-stream (devices): `GET /v1/device/ws` on a real uvicorn server with a `websockets` client - the signed
upgrade, refused keys, hints (contracts/ws/device/), heartbeats → `device_presence` and `device.capacity`, the
presence signals and audit rows, the offline timeout, invalid frames, superseded sockets, overflow and shutdown
(03-api.md section 6.2, 02-data-model.md section 2.1)."""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any

import pytest
from websockets.exceptions import InvalidStatus

from agent_hub.stream import presence
from agent_hub.stream.publish import hint
from agent_hub.stream.ws import FrameInvalid, parse_heartbeat
from tests.platform.hub.conftest import HubHarness, contracts_registry
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.stream.support import (
    CAPACITY,
    LiveHub,
    device_key,
    free_port,
    live_hub,
    signed_headers,
    wait_for,
)

HELLO = {"type": "hello", "hub_time": "2026-11-21T12:00:00.123Z"}
Emitted = list[tuple[str, dict[str, Any]]]


def _rows(hub: HubHarness, query: str, *params: object) -> list[tuple[Any, ...]]:
    with hub.pool.connection() as connection:
        return connection.execute(query, params).fetchall()


def _presence(hub: HubHarness, device: uuid.UUID) -> list[tuple[Any, ...]]:
    return _rows(hub, "SELECT hub_instance FROM hub.device_presence WHERE device_id = %s", device)


def _audit(hub: HubHarness, device: uuid.UUID) -> list[tuple[str, dict[str, Any]]]:
    rows = _rows(hub, "SELECT action, detail FROM hub.audit WHERE device_id = %s ORDER BY seq", device)
    for action, detail in rows:
        contracts_registry().validate(f"audit/{action}.json", detail)
    return [(str(action), dict(detail)) for action, detail in rows]


def test_connect_heartbeat_disconnect(hub: HubHarness, live: LiveHub, emitted: Emitted) -> None:
    key = hub.keys.device()
    device = key.principal.principal_id
    assert device is not None
    client = live.device(key)
    assert client.hint() == HELLO
    assert _presence(hub, device) == [(live.service.instance,)]
    assert wait_for(lambda: ("device.presence", {"device_id": device, "online": 1}) in emitted)
    client.heartbeat(
        sessions=[{"session_id": new_id(), "state": "working"}], vms=[{"vm_id": "vm-0123abcd", "state": "running"}]
    )
    capacity = wait_for(lambda: _rows(hub, "SELECT capacity FROM hub.device WHERE id = %s", device)[0][0] == CAPACITY)
    assert capacity
    assert wait_for(lambda: ("device.capacity", {"device_id": device}) in emitted)
    client.heartbeat()
    client.heartbeat({**CAPACITY, "vm_slots_free": 3})
    assert wait_for(lambda: [name for name, _ in emitted].count("device.capacity") == 2)
    with hub.pool.connection() as connection:
        assert presence.is_online(connection, device, hub.clock(), hub.config.device_online_seconds)
        assert presence.online_devices(connection, [], hub.clock(), 30) == set()
    client.close()
    assert wait_for(lambda: _presence(hub, device) == [])
    assert wait_for(lambda: ("device.presence", {"device_id": device, "online": 0}) in emitted)
    assert _audit(hub, device) == [("device.connect", {"channel": "ws"}), ("device.disconnect", {"channel": "ws"})]


def test_reconnect_within_the_window_is_not_a_presence_change(hub: HubHarness, live: LiveHub, emitted: Emitted) -> None:
    key = hub.keys.device()
    device = key.principal.principal_id
    assert device is not None
    first = live.device(key)
    assert first.hint() == HELLO
    second = live.device(key)
    assert second.hint() == HELLO
    assert first.close_code() == 1008  # superseded
    second.heartbeat()
    assert wait_for(lambda: [name for name, _ in emitted].count("device.capacity") == 1)
    assert [ids["online"] for name, ids in emitted if name == "device.presence"] == [1]
    assert _presence(hub, device) == [(live.service.instance,)]  # the old socket's disconnect left the new row


def test_hints_reach_only_the_addressed_device(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    first = live.device(device_key(hub.keys, scene.device, scene.org))
    second = live.device(device_key(hub.keys, scene.other_device, scene.org))
    assert first.hint() == HELLO
    assert second.hint() == HELLO
    session = new_id()
    hint(uuid.UUID(scene.device.id), "session.start", {"session_id": session})
    assert first.hint() == {"type": "session.start", "session_id": session}
    response = scene.post(scene.writer, scene.wg_event(scene.writer))
    seq = response.json()["results"][0]["scope_seq"]
    events = {"type": "events", "scope_id": scene.wg, "scope_seq": seq}
    assert first.hint() == events
    assert second.hint() == events
    hint(uuid.UUID(scene.other_device.id), "reconcile")
    assert second.hint() == {"type": "reconcile"}
    with pytest.raises(TimeoutError):
        first.hint(timeout=0.3)
    hint(uuid.UUID(new_id()), "reconcile")  # a device that is not connected: dropped, never queued


def test_refused_keys_and_devices(hub: HubHarness, live: LiveHub) -> None:
    refused = [
        hub.keys.device(state="pending_certification"),
        hub.keys.device(state="decertified"),
        hub.keys.device(state="removed"),
        hub.keys.session(),
    ]
    for key in refused:
        with pytest.raises(InvalidStatus) as error:
            live.device(key)
        assert error.value.response.status_code == 403
    good = hub.keys.device()
    headers = signed_headers(good, live.clock, "/v1/device/ws")
    with pytest.raises(InvalidStatus):
        live.device(good, headers={**headers, "Agent-Signature": headers["Agent-Signature"][::-1]})
    with pytest.raises(InvalidStatus):
        live.device(good, headers={})
    with pytest.raises(InvalidStatus):
        live.device(good, headers=headers | {"Agent-Key": "device." + "0" * 32})


def test_decertified_hint_closes_the_socket(hub: HubHarness, live: LiveHub) -> None:
    key = hub.keys.device()
    client = live.device(key)
    assert client.hint() == HELLO
    hint(key.principal.principal_id, "decertified")  # type: ignore[arg-type]
    assert client.hint() == {"type": "decertified"}
    assert client.close_code() == 1008


def test_a_device_that_is_no_longer_active_is_closed_at_its_heartbeat(hub: HubHarness, live: LiveHub) -> None:
    key = hub.keys.device()
    client = live.device(key)
    assert client.hint() == HELLO
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.device SET state = 'removed' WHERE id = %s", (key.principal.principal_id,))
    client.heartbeat()
    assert client.close_code() == 1008


def test_a_presence_row_taken_over_closes_the_socket(hub: HubHarness, live: LiveHub) -> None:
    key = hub.keys.device()
    device = key.principal.principal_id
    assert device is not None
    client = live.device(key)
    assert client.hint() == HELLO
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.device_presence SET hub_instance = 'other' WHERE device_id = %s", (device,))
    client.heartbeat()
    assert client.close_code() == 1008
    assert wait_for(lambda: device not in live.service.devices)
    assert [action for action, _ in _audit(hub, device)] == ["device.connect"]  # the row was not this socket's
    assert _presence(hub, device) == [("other",)]


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (b"\x00binary", 1003),
        ("{", 1008),
        (json.dumps({"type": "status"}), 1008),
        ("x" * (64 * 1024 + 1), 1009),
    ],
)
def test_invalid_frames_close_the_socket(hub: HubHarness, live: LiveHub, frame: str | bytes, code: int) -> None:
    client = live.device(hub.keys.device())
    assert client.hint() == HELLO
    client.send_raw(frame)
    assert client.close_code() == code


def test_missing_heartbeats_close_the_socket(hub: HubHarness, emitted: Emitted) -> None:
    config = dataclasses.replace(hub.config, device_online_seconds=1)
    with live_hub(hub, free_port(16), config=config) as served:
        key = hub.keys.device()
        device = key.principal.principal_id
        client = served.device(key)
        assert client.hint() == HELLO
        assert client.close_code(timeout=5) == 1008
        assert wait_for(lambda: _presence(hub, device) == [])  # type: ignore[arg-type]
        assert wait_for(lambda: ("device.presence", {"device_id": device, "online": 0}) in emitted)


def test_hint_overflow_leaves_one_reconcile(hub: HubHarness, live: LiveHub) -> None:
    live.service.hint_buffer = 2
    key = hub.keys.device()
    client = live.device(key)
    assert client.hint() == HELLO
    device = key.principal.principal_id
    sessions = [new_id() for _ in range(5)]

    def burst() -> None:
        socket = live.service.devices[device]  # type: ignore[index]
        for session in sessions:
            socket.offer("session.start", {"session_id": session})

    live.service.loop.call_soon_threadsafe(burst)  # type: ignore[union-attr]
    assert client.hint() == {"type": "reconcile"}
    assert client.hint() == {"type": "session.start", "session_id": sessions[-1]}


def test_shutdown_closes_sockets_and_refuses_new_ones(hub: HubHarness, live: LiveHub) -> None:
    key = hub.keys.device()
    client = live.device(key)
    assert client.hint() == HELLO
    live.service.loop.call_soon_threadsafe(live.service.close_all)  # type: ignore[union-attr]
    assert client.close_code() == 1012
    with pytest.raises(InvalidStatus):
        live.device(key)


def test_heartbeat_parser_refuses_everything_else() -> None:
    good = {"type": "heartbeat", "capacity": dict(CAPACITY), "sessions": [], "vms": []}
    assert parse_heartbeat(json.dumps(good)) == CAPACITY
    session = {"session_id": new_id(), "state": "idle"}
    invalid: list[Any] = [
        [],
        {**good, "type": "status"},
        {**good, "extra": 1},
        {**good, "capacity": []},
        {**good, "capacity": {**CAPACITY, "more": 1}},
        {**good, "capacity": {**CAPACITY, "pool_ready": -1}},
        {**good, "capacity": {**CAPACITY, "pool_ready": True}},
        {**good, "capacity": {**CAPACITY, "pool_ready": 2**53}},
        {**good, "sessions": {}},
        {**good, "sessions": [session] * 1001},
        {**good, "sessions": ["x"]},
        {**good, "sessions": [{**session, "more": 1}]},
        {**good, "sessions": [{**session, "session_id": "nope"}]},
        {**good, "sessions": [{**session, "session_id": 7}]},
        {**good, "sessions": [{**session, "state": "gone"}]},
        {**good, "vms": [{"vm_id": "vm-xyz", "state": "running"}]},
    ]
    for frame in invalid:
        with pytest.raises(FrameInvalid):
            parse_heartbeat(json.dumps(frame))
    with pytest.raises(FrameInvalid):
        parse_heartbeat("not json")


def test_an_org_device_removed_from_its_org_is_closed_at_its_heartbeat(
    scene: Scene, hub: HubHarness, live: LiveHub
) -> None:
    client = live.device(device_key(hub.keys, scene.device, scene.org))
    assert client.hint() == HELLO
    device = uuid.UUID(scene.device.id)
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.org_member SET removed_seq = 999 WHERE org_id = %s AND principal_id = %s", (scene.org, device)
        )
    client.heartbeat()
    assert client.close_code() == 1008
    presence = "SELECT action, org_id::text FROM hub.audit WHERE device_id = %s AND action LIKE 'device.%%connect'"
    assert wait_for(lambda: len(_rows(hub, presence, device)) == 2)
    orgs = _rows(hub, presence + " ORDER BY seq", device)
    assert orgs == [("device.connect", scene.org), ("device.disconnect", scene.org)]
