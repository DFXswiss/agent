"""U-HUB-github (ADR-076 section 3): defaults of organizations and workgroups. Org admins set the org default, org
admins and writers of a workgroup the workgroup default; only a company record of that very organization can be a
default (`identity_forbidden`, also for a personal record: A15); the feature gate refuses a default while a device of
the org runs an older runner; a default goes away with its record; the device state carries the defaults."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.github.support import NO_CONTRACT, Studio
from tests.platform.hub.orgs.support import new_id


def _put(caller: Any, scope: str, scope_id: str, identity: str | None, **options: Any) -> Any:
    return caller.browser.put(f"/v1/github-defaults/{scope}/{scope_id}", json={"identity_id": identity}, **options)


def _reconciles(published: MemoryPublisher) -> list[str]:
    return sorted(str(item.audience.id) for item in published.items() if item.frame == "reconcile")


def test_an_admin_sets_and_clears_the_org_default(studio: Studio, published: MemoryPublisher) -> None:
    admin, writer = studio.scene.admin, studio.scene.writer
    studio.announce()
    record_id = studio.created_company()["header"]["id"]
    published.clear()
    response = _put(admin, "org", studio.org, record_id)
    assert (response.status_code, response.json()) == (200, {})
    assert _reconciles(published) == sorted([studio.scene.device.id, studio.scene.other_device.id])
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = studio.audit("github.default")
    assert (actor_kind, str(actor_id), target_kind, target_id) == ("user", admin.id, "org", studio.org)
    assert detail == {"scope": "org", "identity_id": record_id}
    defaults = writer.browser.get("/v1/me/github-identities").json()["defaults"]
    assert defaults == [
        {
            "scope": "org",
            "scope_id": studio.org,
            "identity_id": record_id,
            "set_by": admin.id,
            "set_at": "2026-11-21T12:00:00.123Z",
        }
    ]
    state = studio.desk.client(studio.scene.device).get("/v1/device/state").json()
    assert state["github_defaults"] == [{"scope": "org", "scope_id": studio.org, "identity_id": record_id}]
    published.clear()
    assert _put(admin, "org", studio.org, record_id).status_code == 200  # unchanged
    assert len(studio.audit("github.default")) == 1
    assert _reconciles(published) == []
    assert _put(admin, "org", studio.org, None).status_code == 200
    assert studio.rows("SELECT count(*) FROM hub.github_default") == [(0,)]
    assert studio.audit("github.default")[-1][5] == {"scope": "org", "identity_id": None}
    assert _put(admin, "org", studio.org, None).status_code == 200  # nothing to clear
    assert len(studio.audit("github.default")) == 2


def test_writers_and_admins_set_the_workgroup_default(studio: Studio) -> None:
    admin, writer, reader = studio.scene.admin, studio.scene.writer, studio.scene.reader
    studio.announce()
    first, second = studio.created_company()["header"]["id"], studio.created_company()["header"]["id"]
    assert _put(writer, "wg", studio.scene.wg, first).status_code == 200
    [(_kind, _actor, _device, target_kind, target_id, detail)] = studio.audit("github.default")
    assert (target_kind, target_id, detail) == ("workgroup", studio.scene.wg, {"scope": "wg", "identity_id": first})
    assert studio.rows("SELECT wg_id::text, org_id::text FROM hub.audit WHERE action = 'github.default'") == [
        (studio.scene.wg, studio.org)
    ]
    assert _put(admin, "wg", studio.scene.wg, second).status_code == 200
    assert studio.rows("SELECT identity_id::text, set_by::text FROM hub.github_default") == [(second, admin.id)]
    refused = _put(reader, "wg", studio.scene.wg, first)
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    for user in (writer, reader):
        listed = user.browser.get("/v1/me/github-identities").json()["defaults"]
        assert [(item["scope"], item["identity_id"]) for item in listed] == [("wg", second)]
    state = studio.desk.client(studio.scene.device).get("/v1/device/state").json()
    assert state["github_defaults"] == [{"scope": "wg", "scope_id": studio.scene.wg, "identity_id": second}]


@pytest.mark.parametrize(
    "case", ["writer_org", "stranger_org", "unknown_org", "unknown_wg", "bad_scope", "bad_scope_id", "stranger_wg"]
)
def test_refused_scopes(studio: Studio, case: str) -> None:
    writer, stranger = studio.scene.writer, studio.scene.stranger
    record_id = studio.created_company()["header"]["id"]
    caller, scope, scope_id = {
        "writer_org": (writer, "org", studio.org),
        "stranger_org": (stranger, "org", studio.org),
        "unknown_org": (writer, "org", new_id()),
        "unknown_wg": (writer, "wg", new_id()),
        "bad_scope": (writer, "task", studio.org),
        "bad_scope_id": (writer, "org", "not-a-uuid"),
        "stranger_wg": (stranger, "wg", studio.scene.wg),
    }[case]
    response = _put(caller, scope, scope_id, record_id, extensions=NO_CONTRACT)
    expected = (403, "forbidden") if case in ("writer_org", "stranger_org", "stranger_wg") else (404, "not_found")
    assert (response.status_code, response.json()) == (expected[0], {"error": expected[1]})
    assert studio.rows("SELECT count(*) FROM hub.github_default") == [(0,)]


@pytest.mark.parametrize("case", ["personal", "other_org", "unknown"])
def test_only_a_company_record_of_the_org_can_be_a_default(studio: Studio, case: str) -> None:
    admin = studio.scene.admin
    studio.announce()
    identity = {
        "personal": lambda: studio.created(admin)["header"]["id"],
        "other_org": lambda: studio.raw_identity(studio.scene.stranger.id, owner_kind="org"),
        "unknown": new_id,
    }[case]()
    for scope, scope_id in (("org", studio.org), ("wg", studio.scene.wg)):
        response = _put(admin, scope, scope_id, identity)
        assert (response.status_code, response.json()) == (403, {"error": "identity_forbidden"})
    assert studio.rows("SELECT count(*) FROM hub.github_default") == [(0,)]


def test_the_feature_gate_refuses_a_default_while_a_device_is_outdated(studio: Studio) -> None:
    admin, device, other = studio.scene.admin, studio.scene.device, studio.scene.other_device
    studio.announce(device)
    record_id = studio.created_company()["header"]["id"]
    response = _put(admin, "org", studio.org, record_id)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "peer_outdated", "detail": {"devices": [other.id]}},
    )
    assert _put(admin, "org", studio.org, None).status_code == 200  # clearing is always allowed
    studio.announce(other, features=("log.admit", "github.identities"))
    assert _put(admin, "org", studio.org, record_id).status_code == 200


def test_a_default_goes_away_with_its_record(studio: Studio) -> None:
    admin = studio.scene.admin
    studio.announce()
    record_id = studio.created_company()["header"]["id"]
    assert _put(admin, "org", studio.org, record_id).status_code == 200
    assert _put(admin, "wg", studio.scene.wg, record_id).status_code == 200
    assert admin.browser.delete(f"/v1/github-identities/{record_id}").status_code == 200
    assert studio.rows("SELECT count(*) FROM hub.github_default") == [(0,)]
    assert studio.desk.client(studio.scene.device).get("/v1/device/state").json()["github_defaults"] == []
