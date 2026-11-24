"""U-HUB-github (ADR-078 section 1, key model KM-35): company identities of an organization. Only an active admin
writes one, each version under a fresh epoch of the org credential key whose wraps go to every reader device (the
devices assigned to a workgroup of the org; no user, not even the admin); members see the metadata and the
ciphertext, strangers nothing; a forged write by a member is refused."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.github.support import NO_CONTRACT, Studio
from tests.platform.hub.orgs.support import new_id


def _frames(published: MemoryPublisher, frame: str) -> list[tuple[str, dict[str, Any]]]:
    return sorted((str(item.audience.id), dict(item.data)) for item in published.items() if item.frame == frame)


def test_an_admin_writes_a_company_record_with_a_new_key_epoch(studio: Studio, published: MemoryPublisher) -> None:
    admin, device, other = studio.scene.admin, studio.scene.device, studio.scene.other_device
    record = studio.company()
    assert record["header"]["epoch"] == 1
    response = studio.put_company(record, 0)
    assert (response.status_code, response.json()) == (200, {"version": 1})
    stored = studio.rows(
        "SELECT owner_id::text, owner_kind, is_primary, epoch FROM hub.credential WHERE id = %s", record["header"]["id"]
    )
    assert stored == [(studio.org, "org", False, 1)]
    assert studio.org_epoch() == 1
    wraps = studio.rows(
        "SELECT recipient_id::text, wrapped_by::text FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s"
        " ORDER BY recipient_id",
        studio.org,
    )
    assert wraps == sorted([(device.id, admin.id), (other.id, admin.id)])
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = studio.audit("credential.put")
    assert (actor_kind, str(actor_id), target_kind, target_id) == (
        "user",
        admin.id,
        "credential",
        record["header"]["id"],
    )
    assert detail == {"kind": "github", "version": 1, "owner_kind": "org"}
    assert studio.rows("SELECT org_id::text FROM hub.audit WHERE action = 'credential.put'") == [(studio.org,)]
    readers = sorted([device.id, other.id])
    assert _frames(published, "credentials") == [(item, {"owner_id": studio.org}) for item in readers]
    assert _frames(published, "wraps") == [(item, {}) for item in readers]
    # the next version comes with the next epoch
    update = studio.company(record_id=record["header"]["id"], version=2)
    assert update["header"]["epoch"] == 2
    assert studio.put_company(update, 1).json() == {"version": 2}
    assert studio.rows("SELECT version, epoch FROM hub.credential WHERE id = %s", record["header"]["id"]) == [(2, 2)]
    assert studio.put_company(update, 1).json() == {"version": 2}  # an identical retry
    assert studio.org_epoch() == 2


def test_members_see_company_records_and_strangers_do_not(studio: Studio) -> None:
    writer, reader, stranger = studio.scene.writer, studio.scene.reader, studio.scene.stranger
    record = studio.created_company()
    record_id = record["header"]["id"]
    for member in (writer, reader, studio.scene.admin):
        got = member.browser.get(f"/v1/github-identities/{record_id}")
        assert got.json() == {"owner_id": studio.org, "owner_kind": "org", "primary": False, "record": record}
        listed = member.browser.get("/v1/me/github-identities").json()["identities"]
        assert [(item["id"], item["owner_kind"]) for item in listed] == [(record_id, "org")]
    assert stranger.browser.get(f"/v1/github-identities/{record_id}").status_code == 404
    assert stranger.browser.get("/v1/me/github-identities").json() == {"identities": [], "defaults": []}
    # no user receives a wrap of the org credential key
    users = [studio.scene.admin.id, writer.id, reader.id]
    assert studio.rows(
        "SELECT count(*) FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND recipient_id = ANY(%s::uuid[])",
        studio.org,
        users,
    ) == [(0,)]


def test_a_member_cannot_forge_a_company_record(studio: Studio) -> None:
    writer = studio.scene.writer
    forged = studio.company(author=writer)
    response = studio.put_company(forged, 0, caller=writer, key=studio.key(1, wrapper=writer))
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    unknown = studio.put(writer, studio.personal(writer), 0, owner_id=new_id())
    assert (unknown.status_code, unknown.json()) == (403, {"error": "forbidden"})
    assert studio.rows("SELECT count(*) FROM hub.credential") == [(0,)]
    assert studio.org_epoch() == 0


@pytest.mark.parametrize(
    "case",
    ["no_key", "epoch_skips", "epoch_repeats", "missing_reader", "wrong_scope", "user_recipient", "header_epoch"],
)
def test_refused_company_writes(studio: Studio, case: str) -> None:
    admin, device = studio.scene.admin, studio.scene.device
    record = studio.company()
    expected: tuple[int, dict[str, Any]]
    if case == "no_key":
        response = studio.put(admin, record, 0)
        expected = (400, {"error": "request_invalid", "detail": {"pointers": ["/key"]}})
    elif case == "epoch_skips":
        record = studio.company(epoch=2)
        response = studio.put_company(record, 0)
        expected = (409, {"error": "version_conflict", "detail": {"expected": 1}})
    elif case == "epoch_repeats":
        studio.created_company()
        record = studio.company(epoch=1)
        response = studio.put_company(record, 0)
        expected = (409, {"error": "version_conflict", "detail": {"expected": 2}})
    elif case == "missing_reader":
        response = studio.put_company(record, 0, key=studio.key(1, [device]))
        expected = (400, {"error": "request_invalid", "detail": {"pointers": ["/key/wraps"]}})
    elif case == "wrong_scope":
        key = studio.key(1)
        key["wraps"][0] = studio.wrap(admin, 1, device, scope_id=studio.scene.writer.id)
        response = studio.put_company(record, 0, key=key)
        expected = (400, {"error": "request_invalid", "detail": {"pointers": ["/key/wraps/0"]}})
    elif case == "user_recipient":
        key = studio.key(1, [studio.scene.device, studio.scene.other_device, admin])
        response = studio.put_company(record, 0, key=key)
        expected = (400, {"error": "wrap_unauthorized"})
    else:
        response = studio.put_company(record, 0, key=studio.key(2))
        expected = (400, {"error": "epoch_stale"})
    assert (response.status_code, response.json()) == expected
    epochs = studio.rows("SELECT count(*) FROM hub.scope_epoch WHERE scope = 'cred' AND scope_id = %s", studio.org)
    assert epochs == [(1 if case == "epoch_repeats" else 0,)]


def test_only_an_admin_deletes_a_company_record(studio: Studio, published: MemoryPublisher) -> None:
    writer, admin = studio.scene.writer, studio.scene.admin
    record = studio.created_company()
    record_id = record["header"]["id"]
    refused = writer.browser.delete(f"/v1/github-identities/{record_id}")
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    published.clear()
    response = admin.browser.delete(f"/v1/github-identities/{record_id}")
    assert (response.status_code, response.json()) == (200, {})
    assert studio.rows("SELECT count(*) FROM hub.credential") == [(0,)]
    [(_kind, actor, _device, _target_kind, target_id, detail)] = studio.audit("credential.delete")
    assert (str(actor), target_id) == (admin.id, record_id)
    assert detail == {"kind": "github", "version": 1, "owner_kind": "org"}
    assert len(_frames(published, "credentials")) == 2
    gone = admin.browser.get(f"/v1/github-identities/{record_id}", extensions=NO_CONTRACT)
    assert gone.status_code == 404


def test_a_reader_device_wraps_the_org_credential_key_for_a_new_device(studio: Studio) -> None:
    scene, writer = studio.scene, studio.scene.writer
    studio.created_company()
    newcomer = studio.desk.world.device()
    studio.desk.world.join(scene.draft, newcomer, wgs={scene.wg: "device"})
    wrap = studio.wrap(scene.device, 1, newcomer)
    response = studio.desk.client(scene.device).post("/v1/device/wraps", json={"wraps": [wrap]})
    assert response.status_code == 200, response.text
    assert studio.rows(
        "SELECT wrapped_by::text FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND recipient_id = %s",
        studio.org,
        newcomer.id,
    ) == [(scene.device.id,)]
    # a member that is no admin wraps nothing of it, and no user receives it
    refused = writer.browser.post("/v1/wraps", json={"wraps": [studio.wrap(writer, 1, scene.other_device)]})
    assert (refused.status_code, refused.json()) == (400, {"error": "wrap_unauthorized"})
    refused = studio.desk.client(scene.device).post(
        "/v1/device/wraps", json={"wraps": [studio.wrap(scene.device, 1, writer)]}
    )
    assert (refused.status_code, refused.json()) == (400, {"error": "wrap_unauthorized"})
