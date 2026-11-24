"""U-HUB-scheduler (device lists): `GET /v1/workgroups/{wg}`, `GET /v1/workgroups/{wg}/device-candidates` and
`GET /v1/orgs/{org}/devices` - presence, capacity, `capacity_low`, candidate order and rights (03-api.md sections 4.5,
4.6.1 step 1, 4.6.2; ADR-026; R-3.4)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from agent_hub.events.paging import encode_cursor
from agent_hub.workgroups import read
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World, new_id
from tests.platform.hub.tasks.support import CAPACITY, Desk


def capacity(free: int, total: int = 8, used: int = 0, pool: int = 0) -> dict[str, int]:
    return {
        "vm_memory_budget_mib": 65536,
        "vm_memory_used_mib": used,
        "vm_slots_total": total,
        "vm_slots_free": free,
        "pool_ready": pool,
    }


def test_workgroup_lists_devices_and_members(desk: Desk) -> None:
    desk.offline(desk.scene.other_device)
    desk.set_capacity(desk.scene.other_device, capacity(0))
    body = desk.scene.reader.browser.get(f"/v1/workgroups/{desk.wg}").json()
    assert (body["wg_id"], body["org_id"], body["is_default"], body["epoch"]) == (desk.wg, desk.org, False, 1)
    devices = {item["device_id"]: item for item in body["devices"]}
    assert devices[desk.scene.device.id] == {
        "device_id": desk.scene.device.id,
        "kind": "org",
        "online": True,
        "capacity": CAPACITY,
        "capacity_low": False,
    }
    assert devices[desk.scene.other_device.id]["online"] is False
    assert devices[desk.scene.other_device.id]["capacity_low"] is True
    members = {item["user_id"]: item["access"] for item in body["members"]}
    assert members == {desk.scene.admin.id: "write", desk.scene.writer.id: "write", desk.scene.reader.id: "read"}
    default = desk.scene.reader.browser.get(f"/v1/workgroups/{desk.scene.draft.default_wg}").json()
    assert (default["is_default"], default["devices"]) == (True, [])


def test_workgroup_rights(desk: Desk) -> None:
    for path in (f"/v1/workgroups/{desk.wg}", f"/v1/workgroups/{new_id()}", "/v1/workgroups/nope"):
        response = desk.scene.stranger.browser.get(path)
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    response = desk.scene.reader.browser.get(f"/v1/workgroups/{desk.wg}/device-candidates")
    assert response.status_code == 404
    response = desk.scene.stranger.browser.get(f"/v1/orgs/{desk.org}/devices")
    assert response.status_code == 404


def platform_ready(fleet: Fleet, desk: Desk, free: int, *, online: bool = True, used: int = 0) -> Any:
    device, seq = fleet.platform_device()
    desk.set_capacity(device, capacity(free, used=used))
    if online:
        desk.online(device, capacity(free, used=used))
    return device, seq


def test_candidates_are_ordered_by_availability(world: World, fleet: Fleet) -> None:
    draft = world.private_org()
    desk = Desk.build(world)
    low = platform_ready(fleet, desk, 0)
    many = platform_ready(fleet, desk, 6)
    tie_more_memory = platform_ready(fleet, desk, 3)
    tie_less_memory = platform_ready(fleet, desk, 3, used=1000)
    offline = platform_ready(fleet, desk, 8, online=False)
    pending = fleet.platform_device()[0]
    desk.execute("UPDATE hub.device SET certified_seq = NULL WHERE id = %s", pending.id)
    founder = draft.founder.browser
    body = founder.get(f"/v1/workgroups/{draft.default_wg}/device-candidates").json()
    assert [item["device_id"] for item in body["candidates"]] == [
        many[0].id,
        tie_more_memory[0].id,
        tie_less_memory[0].id,
        low[0].id,
        offline[0].id,
    ]
    first = body["candidates"][0]
    assert first["platform_seq"] == many[1]
    assert first["pub"] == many[0].pub
    assert (first["online"], first["capacity_low"]) == (True, False)
    assert body["candidates"][3]["capacity_low"] is True
    assert body["candidates"][4]["online"] is False


def test_assigned_devices_and_orgs_without_platform_access_have_no_candidates(world: World, fleet: Fleet) -> None:
    draft = world.private_org()
    desk = Desk.build(world)
    device, seq = platform_ready(fleet, desk, 4)
    draft.add("device_assign", draft.founder, device=device.id, wg=draft.default_wg, platform_seq=seq)
    response = draft.post(draft.founder, draft.wraps(draft.founder, "wg", draft.default_wg, 1, [device]))
    assert response.status_code == 200, response.text
    assert draft.founder.browser.get(f"/v1/workgroups/{draft.default_wg}/device-candidates").json() == {
        "candidates": []
    }
    # public orgs (no platform access) never see platform candidates
    body = desk.scene.admin.browser.get(f"/v1/workgroups/{desk.wg}/device-candidates").json()
    assert body == {"candidates": []}


def test_org_devices(world: World, fleet: Fleet) -> None:
    draft = world.private_org()
    desk = Desk.build(world)
    device, seq = platform_ready(fleet, desk, 0)
    draft.add("device_assign", draft.founder, device=device.id, wg=draft.default_wg, platform_seq=seq)
    assert draft.post(draft.founder, draft.wraps(draft.founder, "wg", draft.default_wg, 1, [device])).status_code == 200
    seen = desk.hub.clock() - timedelta(seconds=40)
    desk.online(device, capacity(0), seen=seen)
    body = draft.founder.browser.get(f"/v1/orgs/{draft.id}/devices").json()
    assert body == {
        "devices": [
            {
                "device_id": device.id,
                "kind": "platform",
                "state": "active",
                "online": False,
                "last_seen_at": seen.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "agent_version": "0.1.0",
                "capacity": capacity(0),
                "capacity_low": True,
                "assigned_wgs": [draft.default_wg],
            }
        ]
    }
    scene_devices = desk.scene.admin.browser.get(f"/v1/orgs/{desk.org}/devices").json()["devices"]
    found = {item["device_id"]: item for item in scene_devices}
    assert set(found) == {desk.scene.device.id, desk.scene.other_device.id}
    assert found[desk.scene.device.id]["last_seen_at"] is None
    assert found[desk.scene.device.id]["assigned_wgs"] == [desk.wg]
    assert desk.scene.reader.browser.get(f"/v1/orgs/{desk.org}/devices").status_code == 200


def test_org_devices_page_and_are_capped(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-063 section 2 (KOR-35): `before`/`limit` page in the order `enrolled_at DESC, id DESC`; without them at
    most `DEVICES_MAX` devices (`truncated: true` when cut)."""
    admin = desk.scene.admin.browser
    url = f"/v1/orgs/{desk.org}/devices"
    whole = admin.get(url).json()
    order = [item["device_id"] for item in whole["devices"]]
    assert (len(order), set(whole)) == (2, {"devices"})
    first = admin.get(url, params={"limit": "1"}).json()
    assert [item["device_id"] for item in first["devices"]] == order[:1]
    rest = admin.get(url, params={"limit": "1", "before": first["next_before"]}).json()
    assert ([item["device_id"] for item in rest["devices"]], rest["next_before"]) == (order[1:], None)
    monkeypatch.setattr(read, "DEVICES_MAX", 1)
    capped = admin.get(url).json()
    assert ([item["device_id"] for item in capped["devices"]], capped["truncated"]) == (order[:1], True)
    foreign = encode_cursor(desk.hub.clock(), "not-a-uuid")
    response = admin.get(url, params={"before": foreign}, extensions={"contract": False})
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_candidates_page_in_enrolment_order_and_are_capped(
    world: World, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft = world.private_org()
    desk = Desk.build(world)
    devices = [platform_ready(fleet, desk, free)[0].id for free in (1, 6, 3)]
    founder = draft.founder.browser
    url = f"/v1/workgroups/{draft.default_wg}/device-candidates"
    ranked = [item["device_id"] for item in founder.get(url).json()["candidates"]]
    assert ranked == [devices[1], devices[2], devices[0]]
    with desk.hub.pool.connection() as connection:
        rows = connection.execute(
            "SELECT id::text FROM hub.device WHERE id = ANY(%s::uuid[]) ORDER BY enrolled_at DESC, id DESC", (devices,)
        ).fetchall()
    enrolled = [row[0] for row in rows]
    first = founder.get(url, params={"limit": "2"}).json()
    assert [item["device_id"] for item in first["candidates"]] == enrolled[:2]
    rest = founder.get(url, params={"before": first["next_before"]}).json()
    assert ([item["device_id"] for item in rest["candidates"]], rest["next_before"]) == (enrolled[2:], None)
    monkeypatch.setattr(read, "DEVICES_MAX", 2)
    capped = founder.get(url).json()
    assert capped["truncated"] is True
    assert sorted(item["device_id"] for item in capped["candidates"]) == sorted(enrolled[:2])
