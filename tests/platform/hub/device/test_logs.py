"""U-HUB-device-api (logs): `GET /v1/device/logs` and `GET /v1/device/platform-log` (03-api.md section 6.1)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.crypto.encoding import canonical
from agent_hub.log.verify import verify_log
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import World, new_id

NOT_CONTRACT = {"contract": False}


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]


def test_membership_log_of_the_device_org(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    answer = client.get("/v1/device/logs", params={"org": scene.org}).json()
    entries = [item["entry"] for item in answer["entries"]]
    assert [canonical(entry) for entry in entries] == [canonical(entry) for entry in scene.draft.entries]
    assert [item["seq"] for item in answer["entries"]] == list(range(len(entries)))
    assert answer["next_after"] is None
    assert answer["head"]["seq"] == len(entries) - 1
    verified = verify_log(entries, scene.admin.pub["sig"])
    assert verified.head.hash == answer["head"]["hash"]
    page = client.get("/v1/device/logs", params={"org": scene.org, "after": "1", "limit": "2"}).json()
    assert [item["seq"] for item in page["entries"]] == [2, 3]
    assert page["next_after"] == 3
    last = client.get("/v1/device/logs", params={"org": scene.org, "after": str(len(entries) - 1)}).json()
    assert last["entries"] == []
    assert last["next_after"] is None


def test_foreign_or_unknown_org_is_not_found(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    other = world.public_org()
    assert error(client.get("/v1/device/logs", params={"org": other.id})) == (404, "not_found")
    assert error(client.get("/v1/device/logs", params={"org": new_id()})) == (404, "not_found")


def test_log_query_values(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    cases: list[tuple[dict[str, str], str]] = [
        ({}, "/org"),
        ({"org": scene.org.upper()}, "/org"),
        ({"org": scene.org, "after": "-1"}, "/after"),
        ({"org": scene.org, "after": "01"}, "/after"),
        ({"org": scene.org, "limit": "0"}, "/limit"),
        ({"org": scene.org, "limit": "1001"}, "/limit"),
    ]
    for params, pointer in cases:
        response = client.get("/v1/device/logs", params=params, extensions=NOT_CONTRACT)
        assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}, params
    unknown = client.get("/v1/device/logs", params={"org": scene.org, "x": "1"}, extensions=NOT_CONTRACT)
    assert error(unknown) == (400, "request_invalid")
    long_value = client.get("/v1/device/logs", params={"org": "a" * 2000}, extensions=NOT_CONTRACT)
    assert error(long_value) == (400, "request_invalid")


def test_platform_log(fleet: Fleet, world: World) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    assert error(client.get("/v1/device/platform-log")) == (404, "not_found")
    fleet.platform_invite()
    fleet.platform_invite()
    answer = client.get("/v1/device/platform-log").json()
    assert [item["entry"] for item in answer["entries"]] == fleet.platform.entries
    assert answer["head"]["seq"] == 2
    assert answer["next_after"] is None
    page = client.get("/v1/device/platform-log", params={"after": "0", "limit": "1"}).json()
    assert [item["seq"] for item in page["entries"]] == [1]
    assert page["next_after"] == 1
    bad = client.get("/v1/device/platform-log", params={"limit": "x"}, extensions=NOT_CONTRACT)
    assert bad.json() == {"error": "request_invalid", "detail": {"pointers": ["/limit"]}}


def test_log_reads_racing_an_append_serve_entries_and_head_of_one_state(
    world: World, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The head is read before the entries: an append committed in between must not show up beyond the served head.
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    count = len(scene.draft.entries)
    read_entries = logs.entries

    def append_first(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        monkeypatch.setattr(logs, "entries", read_entries)
        draft, admin = scene.draft, scene.admin
        draft.add("wg_create", admin, wg=(wg := new_id()), epoch=1)
        assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin])).status_code == 200
        return read_entries(*args, **kwargs)

    monkeypatch.setattr(logs, "entries", append_first)
    raced = client.get("/v1/device/logs", params={"org": scene.org}).json()
    assert [item["seq"] for item in raced["entries"]] == list(range(count))
    assert (raced["head"]["seq"], raced["next_after"]) == (count - 1, None)
    fleet.platform_invite()
    read_platform = platform_log.stored_entries

    def grant_first(*args: Any, **kwargs: Any) -> list[tuple[int, dict[str, object]]]:
        monkeypatch.setattr(platform_log, "stored_entries", read_platform)
        fleet.platform_invite()
        return read_platform(*args, **kwargs)

    monkeypatch.setattr(platform_log, "stored_entries", grant_first)
    platform = client.get("/v1/device/platform-log").json()
    assert ([item["seq"] for item in platform["entries"]], platform["head"]["seq"]) == ([0, 1], 1)


def test_reads_are_logged(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    fleet.client(scene.device).get("/v1/device/logs", params={"org": scene.org})
    fleet.client(scene.device).get("/v1/device/state")
    fleet.hub.flush_read_log()
    rows = fleet.rows(
        "SELECT action, actor_kind, actor_id::text FROM hub.audit WHERE action LIKE 'read.device.%%' ORDER BY seq"
    )
    assert rows == [
        ("read.device.logs", "device", scene.device.id),
        ("read.device.state", "device", scene.device.id),
    ]
