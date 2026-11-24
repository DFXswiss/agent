"""U-HUB-device-api (wraps): `GET/POST /v1/device/wraps` with the device as recipient and as wrapper (W1-W3,
04-crypto.md section 6.3)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import World, new_id

NOT_CONTRACT = {"contract": False}


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]


def test_device_lists_its_wraps(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    draft, admin, device = scene.draft, scene.admin, scene.device
    wraps = [draft.wrap(admin, "org", scene.org, 1, device), draft.wrap(admin, "wg", scene.wg, 1, device)]
    stored = admin.browser.post("/v1/wraps", json={"wraps": wraps})
    assert stored.json() == {"stored": 2, "ignored": 0}
    client = fleet.client(device)
    listed = client.get("/v1/device/wraps").json()["wraps"]
    assert sorted((item["scope"], item["scope_id"], item["epoch"]) for item in listed) == sorted(
        [("org", scene.org, 1), ("wg", scene.wg, 1)]
    )
    assert {item["recipient_id"] for item in listed} == {device.id}
    assert {item["wrapped_by"] for item in listed} == {admin.id}
    only_wg = client.get("/v1/device/wraps", params={"scope": "wg", "scope_id": scene.wg, "epoch": "1"}).json()
    assert [item["wrap"] for item in only_wg["wraps"]] == [wraps[1]["wrap"]]
    assert client.get("/v1/device/wraps", params={"epoch": "2"}).json() == {"wraps": []}
    assert fleet.client(scene.other_device).get("/v1/device/wraps").json() == {"wraps": []}


def test_wrap_query_values(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    for params, pointer in (
        ({"scope": "task"}, "/scope"),
        ({"scope_id": "x"}, "/scope_id"),
        ({"epoch": "0"}, "/epoch"),
        ({"epoch": "3000000000"}, "/epoch"),
    ):
        response = client.get("/v1/device/wraps", params=params, extensions=NOT_CONTRACT)
        assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}, params


def test_device_wraps_for_entitled_principals(world: World, fleet: Fleet, published: MemoryPublisher) -> None:
    scene = org_device_scene(world)
    draft, device = scene.draft, scene.device
    published.clear()
    wraps = [
        draft.wrap(device, "wg", scene.wg, 1, scene.other_device),
        draft.wrap(device, "org", scene.org, 1, scene.writer),
    ]
    response = fleet.client(device).post("/v1/device/wraps", json={"wraps": wraps})
    assert response.json() == {"stored": 2, "ignored": 0}
    assert fleet.rows(
        "SELECT recipient_id::text, wrapped_by::text FROM hub.key_wrap WHERE wrapped_by = %s ORDER BY recipient_id",
        device.id,
    ) == sorted([(scene.other_device.id, device.id), (scene.writer.id, device.id)])
    frames = sorted(((item.audience.kind, str(item.audience.id)), item.frame) for item in published.items())
    assert frames == sorted([(("device", scene.other_device.id), "wraps"), (("user", scene.writer.id), "wraps")])
    again = fleet.client(device).post("/v1/device/wraps", json={"wraps": wraps})
    assert again.json() == {"stored": 0, "ignored": 2}
    audited = fleet.rows(
        "SELECT actor_kind, device_id::text, detail FROM hub.audit WHERE action = 'wrap.put' AND actor_id = %s"
        " ORDER BY seq",
        device.id,
    )
    assert audited == [
        ("device", device.id, {"stored": 2, "ignored": 0}),
        ("device", device.id, {"stored": 0, "ignored": 2}),
    ]
    assert Audience("device", UUID(scene.other_device.id)) in [item.audience for item in published.items()]


def test_device_wrap_refusals(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    draft, device = scene.draft, scene.device
    stranger = draft.wrap(device, "wg", scene.wg, 1, scene.stranger)
    response = fleet.client(device).post("/v1/device/wraps", json={"wraps": [stranger]})
    assert error(response) == (400, "wrap_unauthorized")
    foreign_scope = draft.wrap(device, "wg", draft.default_wg, 1, scene.writer)
    assert error(fleet.client(device).post("/v1/device/wraps", json={"wraps": [foreign_scope]})) == (
        400,
        "wrap_unauthorized",
    )
    unknown = draft.wrap(device, "wg", new_id(), 1, scene.writer)
    assert error(fleet.client(device).post("/v1/device/wraps", json={"wraps": [unknown]})) == (
        400,
        "wrap_unauthorized",
    )
    empty = fleet.client(device).post("/v1/device/wraps", json={"wraps": []}, extensions=NOT_CONTRACT)
    assert error(empty) == (400, "request_invalid")
    assert fleet.rows("SELECT count(*) FROM hub.key_wrap WHERE wrapped_by = %s", device.id) == [(0,)]
