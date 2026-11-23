"""U-HUB-requests (R-9.1, R-5.3, R-5.1): `GET /v1/me/devices` (03-api.md section 4.1) and zero knowledge of the
credential and request flows (04-crypto.md section 7): a canary inside every plaintext never reaches hub storage."""

from __future__ import annotations

from datetime import timedelta

from tests.platform.hub.credentials.support import CANARY, Desk, database_dump


def test_my_devices_lists_eligible_devices_with_presence(desk: Desk) -> None:
    scene = desk.scene
    now = desk.hub.clock()
    with desk.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.device_presence (device_id, hub_instance, connected_at, last_heartbeat_at)"
            " VALUES (%s, 'test', %s, %s), (%s, 'test', %s, %s)",
            (
                scene.device.id,
                now,
                now - timedelta(seconds=5),
                scene.other_device.id,
                now - timedelta(minutes=5),
                now - timedelta(minutes=2),
            ),
        )
    body = scene.writer.browser.get("/v1/me/devices").json()
    by_id = {item["device_id"]: item for item in body["devices"]}
    assert sorted(by_id) == sorted([scene.device.id, scene.other_device.id])
    assert by_id[scene.device.id] == {
        "device_id": scene.device.id,
        "kind": "org",
        "org_id": scene.org,
        "wg_ids": [scene.wg],
        "online": True,
        "last_seen_at": "2026-11-21T11:59:55.123Z",
    }
    assert by_id[scene.other_device.id]["online"] is False
    assert by_id[scene.other_device.id]["last_seen_at"] == "2026-11-21T11:58:00.123Z"
    assert [item["device_id"] for item in body["devices"]] == sorted(by_id, reverse=True)
    assert scene.stranger.browser.get("/v1/me/devices").json() == {"devices": []}


def test_a_device_without_presence_is_offline(desk: Desk) -> None:
    body = desk.scene.reader.browser.get("/v1/me/devices").json()
    assert {(item["online"], item["last_seen_at"]) for item in body["devices"]} == {(False, None)}


def test_canaries_never_reach_hub_storage(desk: Desk) -> None:
    scene = desk.scene
    writer, device = scene.writer, scene.device
    assert desk.put(writer, desk.record(writer), 0).status_code == 200
    envelope = desk.request(writer, device, "claude.login")
    assert desk.send(writer, envelope).status_code == 200
    request_id = envelope["header"]["id"]
    client = desk.client(device)
    assert client.get(f"/v1/device/requests/{request_id}").status_code == 200
    response = desk.response(device, writer, request_id, "claude.login", body={"step": "authorize", "url": CANARY})
    assert client.post(f"/v1/device/requests/{request_id}/responses", json={"envelope": response}).status_code == 200
    record = desk.record(writer, "claude_subscription", author=device, body={"token": CANARY, "kind": "setup_token"})
    put = client.put(
        f"/v1/device/credentials/{writer.id}/claude_subscription",
        json={"record": record, "expected_version": 0, "request_id": request_id},
    )
    assert put.status_code == 200, put.text
    desk.hub.flush_read_log()
    dump = database_dump(desk)
    assert dump
    assert not [text for text in dump if CANARY in text or CANARY.encode().hex() in text]
