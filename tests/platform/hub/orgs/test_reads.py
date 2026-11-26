"""U-HUB-rights and the read endpoints: `GET /v1/orgs/{org}`, `GET /v1/orgs/{org}/log` and the rights API
(`agent_hub.orgs.rights`, 03-api.md sections 1, 3 and 4.3)."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from agent_hub.errors import HubError
from agent_hub.orgs import logs, rights
from tests.platform.hub.orgs.support import World, new_id


def test_org_view_shows_the_member_its_access(world: World) -> None:
    draft = world.private_org()
    admin = draft.founder
    member, device = world.user(), world.device()
    world.join(draft, member)
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    draft.add("grant", admin, subject=member.id, wg=wg, access="read")
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin, member])).status_code == 200
    archived = new_id()
    draft.add("wg_create", admin, wg=archived, epoch=1)
    draft.add("wg_archive", admin, wg=archived)
    assert draft.post(admin, draft.wraps(admin, "wg", archived, 1, [admin])).status_code == 200
    world.join(draft, device, wgs={wg: "device"})
    view = member.browser.get(f"/v1/orgs/{draft.id}").json()
    assert view["org_id"] == draft.id
    assert (view["private"], view["platform_access"], view["default_wg"]) == (True, True, draft.default_wg)
    assert view["log_head"]["seq"] == 8
    by_id = {item["wg_id"]: item for item in view["workgroups"]}
    assert by_id[draft.default_wg] == {
        "wg_id": draft.default_wg,
        "is_default": True,
        "archived": False,
        "my_access": "write",
        "devices": [],
        "epoch": 1,
    }
    assert (by_id[wg]["my_access"], by_id[wg]["devices"]) == ("read", [device.id])
    assert (by_id[archived]["my_access"], by_id[archived]["archived"]) == (None, True)
    assert [item["wg_id"] for item in view["workgroups"]] == [archived, wg, draft.default_wg]
    assert [(item["principal_id"], item["role"]) for item in view["members"]] == [
        (device.id, "device"),
        (member.id, "member"),
        (admin.id, "admin"),
    ]


def test_org_view_is_not_found_for_strangers_and_unknown_ids(world: World) -> None:
    draft = world.public_org()
    stranger = world.user()
    for path in (f"/v1/orgs/{draft.id}", f"/v1/orgs/{uuid.uuid4()}", "/v1/orgs/NOT-AN-ID", f"/v1/orgs/{draft.id}/log"):
        response = stranger.browser.get(path, extensions={"contract": False})
        assert (response.status_code, response.json()) == (404, {"error": "not_found"}), path
    world.hub.flush_read_log()
    rows = world.rows("SELECT action, result FROM hub.audit WHERE action LIKE 'read.%%' ORDER BY seq")
    assert rows[0] == ("read.org", "denied")


def test_log_pages_ascending_with_next_after(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    for _ in range(3):
        draft.add("wg_create", admin, wg=(wg := new_id()), epoch=1)
        assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin])).status_code == 200
    first = admin.browser.get(f"/v1/orgs/{draft.id}/log", params={"limit": "2"}).json()
    assert [item["seq"] for item in first["entries"]] == [0, 1]
    assert first["entries"][0]["entry"] == draft.entries[0]
    assert (first["next_after"], first["head"]["seq"]) == (1, 3)
    rest = admin.browser.get(f"/v1/orgs/{draft.id}/log", params={"after": "1"}).json()
    assert ([item["seq"] for item in rest["entries"]], rest["next_after"]) == ([2, 3], None)
    for params in ({"limit": "0"}, {"limit": "1001"}, {"after": "-1"}, {"after": "01"}, {"limit": "x"}, {"limit": "²"}):
        response = admin.browser.get(f"/v1/orgs/{draft.id}/log", params=params, extensions={"contract": False})
        assert (response.status_code, response.json()) == (400, {"error": "request_invalid"}), params


def test_a_log_read_racing_an_append_serves_entries_and_head_of_one_state(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The head is read with the org row, the entries afterwards: an append committed in between must not show up as
    # entries beyond the served head (the browser would refuse the page as a broken chain, `log_chain`).
    draft = world.public_org()
    admin = draft.founder
    read_entries = logs.entries

    def append_first(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        monkeypatch.setattr(logs, "entries", read_entries)
        draft.add("wg_create", admin, wg=(wg := new_id()), epoch=1)
        assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin])).status_code == 200
        return read_entries(*args, **kwargs)

    monkeypatch.setattr(logs, "entries", append_first)
    raced = admin.browser.get(f"/v1/orgs/{draft.id}/log").json()
    assert ([item["seq"] for item in raced["entries"]], raced["head"]["seq"], raced["next_after"]) == ([0], 0, None)
    later = admin.browser.get(f"/v1/orgs/{draft.id}/log", params={"after": "0"}).json()
    assert ([item["seq"] for item in later["entries"]], later["head"]["seq"]) == ([1], 1)


def test_rights_api(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member, device, stranger = world.user(), world.device(), world.user()
    world.join(draft, member)
    wg, archived = new_id(), new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    draft.add("grant", admin, subject=member.id, wg=wg, access="write")
    draft.add("wg_create", admin, wg=archived, epoch=1)
    draft.add("grant", admin, subject=member.id, wg=archived, access="write")
    draft.add("wg_archive", admin, wg=archived)
    wraps = draft.wraps(admin, "wg", wg, 1, [admin, member]) + draft.wraps(admin, "wg", archived, 1, [admin, member])
    assert draft.post(admin, wraps).status_code == 200
    world.join(draft, device, wgs={wg: "device"})
    with world.hub.pool.connection() as connection:
        assert rights.org_role(connection, draft.id, admin.id) == "admin"
        assert rights.org_role(connection, draft.id, device.id) == "device"
        assert rights.org_role(connection, draft.id, stranger.id) is None
        assert rights.wg_org(connection, wg) == uuid.UUID(draft.id)
        assert rights.wg_org(connection, new_id()) is None
        assert rights.wg_access(connection, draft.default_wg, member.id) == "write"
        assert rights.wg_access(connection, wg, stranger.id) is None
        assert rights.device_assigned(connection, wg, device.id)
        assert not rights.device_assigned(connection, draft.default_wg, device.id)
        assert rights.wg_archived(connection, archived)
        assert not rights.wg_archived(connection, new_id())
        expectations = [
            (admin, rights.ORG_ADMIN, {"org_id": draft.id}, True),
            (member, rights.ORG_ADMIN, {"org_id": draft.id}, False),
            (member, rights.ORG_MEMBER, {"org_id": draft.id}, True),
            (device, rights.ORG_MEMBER, {"org_id": draft.id}, False),
            (device, rights.DEVICE_ORG, {"org_id": draft.id}, True),
            (member, rights.WG_READ, {"wg_id": wg}, True),
            (member, rights.TASK_WRITE, {"wg_id": wg}, True),
            (admin, rights.WG_WRITE, {"wg_id": wg}, True),
            (member, rights.WG_WRITE, {"wg_id": archived}, False),
            (member, rights.TASK_READ, {"wg_id": archived}, True),
            (stranger, rights.WG_READ, {"wg_id": wg}, False),
            (device, rights.DEVICE_WG, {"wg_id": wg}, True),
            (device, rights.DEVICE_WG, {"wg_id": draft.default_wg}, False),
        ]
        for actor, right, scope, expected in expectations:
            assert rights.can(connection, actor.id, right, **cast(dict[str, Any], scope)) is expected, (right, scope)
        with pytest.raises(ValueError, match="unknown right"):
            rights.can(connection, admin.id, "org:owner", org_id=draft.id)
        with pytest.raises(ValueError, match="missing scope"):
            rights.can(connection, admin.id, rights.WG_READ, org_id=draft.id)
        rights.require(connection, member.id, rights.WG_WRITE, wg_id=wg, write=True)
        refusals = [
            ({"wg_id": archived, "write": True}, "forbidden"),
            ({"wg_id": new_id(), "write": True}, "not_found"),
            ({"wg_id": archived}, "not_found"),
        ]
        for options, code in refusals:
            with pytest.raises(HubError) as refused:
                rights.require(connection, member.id, rights.WG_WRITE, **options)  # type: ignore[arg-type]
            assert refused.value.code == code
        with pytest.raises(HubError) as refused:
            rights.require(connection, member.id, rights.ORG_ADMIN, org_id=draft.id, write=True)
        assert refused.value.code == "forbidden"
        with pytest.raises(HubError) as refused:
            rights.require(connection, member.id, rights.ORG_ADMIN, org_id=new_id(), write=True)
        assert refused.value.code == "not_found"
