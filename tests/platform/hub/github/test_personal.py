"""U-HUB-github (ADR-076 section 1): a user's GitHub identities through `/v1/github-identities` and their compatibility
with the endpoints from before (`/v1/me/credentials`, the rotation): the first record is primary, further ones are
not, the endpoints from before see the primary record only, a deleted primary record promotes none, the primary mark
switches atomically, and every refusal of a record write."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub.credentials import records
from agent_hub.credentials.records import MAX_GITHUB_RECORDS
from agent_hub.errors import HubError
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.github.support import NO_CONTRACT, Studio
from tests.platform.hub.orgs.support import new_id

CREATED = "2026-11-21T12:00:00.123Z"


def _hints(published: MemoryPublisher, frame: str = "credentials") -> list[tuple[str, dict[str, Any]]]:
    return sorted((str(item.audience.id), dict(item.data)) for item in published.items() if item.frame == frame)


def _meta(
    studio: Studio, record: dict[str, Any], *, primary: bool, owner_kind: str = "user", **times: str
) -> dict[str, Any]:
    header = record["header"]
    return {
        "id": header["id"],
        "owner_id": header["owner"],
        "owner_kind": owner_kind,
        "primary": primary,
        "version": header["version"],
        "epoch": header["epoch"],
        "created_at": times.get("created_at", CREATED),
        "updated_at": times.get("updated_at", CREATED),
    }


def test_the_first_record_is_primary_and_the_endpoints_from_before_see_it(
    studio: Studio, published: MemoryPublisher
) -> None:
    writer = studio.scene.writer
    record = studio.personal(writer)
    response = studio.put(writer, record, 0)
    assert (response.status_code, response.json()) == (200, {"version": 1})
    assert studio.primary_of(writer) == [record["header"]["id"]]
    got = writer.browser.get(f"/v1/github-identities/{record['header']['id']}")
    assert got.json() == {"owner_id": writer.id, "owner_kind": "user", "primary": True, "record": record}
    assert writer.browser.get("/v1/me/credentials/github").json() == {"record": record}
    listed = writer.browser.get("/v1/me/github-identities").json()
    assert listed == {"identities": [_meta(studio, record, primary=True)], "defaults": []}
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = studio.audit("credential.put")
    assert (actor_kind, str(actor_id), target_kind, target_id) == (
        "user",
        writer.id,
        "credential",
        record["header"]["id"],
    )
    assert detail == {"kind": "github", "version": 1, "owner_kind": "user"}
    devices = sorted([studio.scene.device.id, studio.scene.other_device.id])
    assert _hints(published) == [(device, {"owner_id": writer.id}) for device in devices]


def test_a_second_record_is_invisible_to_the_endpoints_from_before(studio: Studio) -> None:
    writer = studio.scene.writer
    first = studio.created(writer)
    studio.hub.clock.advance(2)
    second = studio.created(writer)
    assert studio.primary_of(writer) == [first["header"]["id"]]
    assert writer.browser.get("/v1/me/credentials/github").json() == {"record": first}
    legacy = writer.browser.get("/v1/me/credentials").json()["credentials"]
    assert [item["kind"] for item in legacy] == ["github"]
    assert legacy[0]["updated_at"] == CREATED
    listed = writer.browser.get("/v1/me/github-identities").json()["identities"]
    assert listed == [
        _meta(
            studio, second, primary=False, created_at="2026-11-21T12:00:02.123Z", updated_at="2026-11-21T12:00:02.123Z"
        ),
        _meta(studio, first, primary=True),
    ]
    # an update through the endpoint from before addresses the primary record only
    update = studio.personal(writer, record_id=first["header"]["id"], version=2)
    assert studio.desk.put(writer, update, 1).json() == {"version": 2}
    assert writer.browser.get(f"/v1/github-identities/{second['header']['id']}").json()["record"] == second


def test_updates_versions_and_retries(studio: Studio) -> None:
    writer = studio.scene.writer
    record = studio.created(writer)
    record_id = record["header"]["id"]
    assert studio.put(writer, record, 0).json() == {"version": 1}  # an identical retry
    update = studio.personal(writer, record_id=record_id, version=2)
    stale = studio.put(writer, studio.personal(writer, record_id=record_id, version=3), 2)
    assert (stale.status_code, stale.json()) == (409, {"error": "version_conflict", "detail": {"expected": 1}})
    assert studio.put(writer, update, 1).json() == {"version": 2}
    assert studio.rows("SELECT version, is_primary FROM hub.credential WHERE id = %s", record_id) == [(2, True)]
    skipped = studio.put(writer, studio.personal(writer, record_id=record_id, version=4), 2)
    assert skipped.json() == {"error": "request_invalid", "detail": {"pointers": ["/record/header/version"]}}


@pytest.mark.parametrize("case", ["path_id", "other_owner_id", "key_for_personal", "stale_epoch", "unregistered"])
def test_refused_personal_writes(studio: Studio, case: str) -> None:
    writer, reader = studio.scene.writer, studio.scene.reader
    record = studio.personal(writer)
    if case == "path_id":
        response = studio.put(writer, record, 0, path_id=new_id())
        expected = (400, {"error": "request_invalid", "detail": {"pointers": ["/record/header/id"]}})
    elif case == "other_owner_id":
        response = studio.put(reader, record, 0)
        expected = (403, {"error": "forbidden"})
    elif case == "key_for_personal":
        response = studio.put(writer, record, 0, key=studio.key(1))
        expected = (400, {"error": "request_invalid", "detail": {"pointers": ["/key"]}})
    elif case == "stale_epoch":
        studio.desk.ck(writer, 2)
        response = studio.put(writer, record, 0)
        expected = (400, {"error": "epoch_stale"})
    else:
        stranger = studio.desk.world.user(registered=False)
        own = studio.personal(stranger)
        response = studio.put(stranger, own, 0)
        expected = (403, {"error": "forbidden"})
    assert (response.status_code, response.json()) == expected
    assert studio.rows("SELECT count(*) FROM hub.credential") == [(0,)]


def test_an_id_of_another_record_is_an_id_conflict(studio: Studio) -> None:
    writer, reader = studio.scene.writer, studio.scene.reader
    taken = studio.created(reader)
    response = studio.put(writer, studio.personal(writer, record_id=taken["header"]["id"]), 0)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    legacy = studio.desk.record(writer, "anthropic_api_key")
    assert studio.desk.put(writer, legacy, 0).status_code == 200
    response = studio.put(writer, studio.personal(writer, record_id=legacy["header"]["id"]), 0)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})


def test_at_most_twenty_records_per_owner(studio: Studio) -> None:
    writer = studio.scene.writer
    for _ in range(MAX_GITHUB_RECORDS - 1):
        studio.raw_identity(writer.id)
    studio.created(writer)
    response = studio.put(writer, studio.personal(writer), 0)
    assert (response.status_code, response.json()) == (400, {"error": "size_exceeded"})
    # the endpoint from before is held to the same limit when it creates a primary record
    studio.execute("UPDATE hub.credential SET is_primary = false WHERE owner_id = %s", writer.id)
    response = studio.desk.put(writer, studio.desk.record(writer), 0)
    assert (response.status_code, response.json()) == (400, {"error": "size_exceeded"})


def test_the_primary_mark_switches_atomically(studio: Studio, published: MemoryPublisher) -> None:
    writer = studio.scene.writer
    first, second = studio.created(writer), studio.created(writer)
    published.clear()
    response = writer.browser.put(f"/v1/github-identities/{second['header']['id']}/primary", json={})
    assert (response.status_code, response.json()) == (200, {})
    assert studio.primary_of(writer) == [second["header"]["id"]]
    assert writer.browser.get("/v1/me/credentials/github").json() == {"record": second}
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = studio.audit("github.primary")
    assert (actor_kind, str(actor_id), target_kind, target_id) == ("user", writer.id, "user", writer.id)
    assert detail == {"identity_id": second["header"]["id"]}
    assert len(_hints(published)) == 2
    again = writer.browser.put(f"/v1/github-identities/{second['header']['id']}/primary", json={})
    assert again.status_code == 200
    assert len(studio.audit("github.primary")) == 1
    assert first["header"]["id"] not in studio.primary_of(writer)


def test_the_primary_mark_of_foreign_and_company_records_is_refused(studio: Studio) -> None:
    writer, reader = studio.scene.writer, studio.scene.reader
    studio.announce()
    foreign = studio.created(reader)
    company = studio.created_company()
    for record_id, status, error in (
        (foreign["header"]["id"], 404, "not_found"),
        (new_id(), 404, "not_found"),
        (company["header"]["id"], 403, "forbidden"),
    ):
        response = writer.browser.put(f"/v1/github-identities/{record_id}/primary", json={})
        assert (response.status_code, response.json()) == (status, {"error": error})
    response = writer.browser.put("/v1/github-identities/not-a-uuid/primary", json={}, extensions=NO_CONTRACT)
    assert response.status_code == 404


def test_deleting_the_primary_record_promotes_none(studio: Studio, published: MemoryPublisher) -> None:
    writer = studio.scene.writer
    first, second = studio.created(writer), studio.created(writer)
    published.clear()
    response = writer.browser.delete(f"/v1/github-identities/{first['header']['id']}")
    assert (response.status_code, response.json()) == (200, {})
    assert studio.primary_of(writer) == []
    assert writer.browser.get("/v1/me/credentials/github").status_code == 404
    assert writer.browser.get("/v1/me/credentials").json() == {"credentials": []}
    assert studio.desk.client(studio.scene.device).get(f"/v1/device/credentials/{writer.id}/github").status_code == 404
    [(_kind, _actor, _device, _target_kind, target_id, detail)] = studio.audit("credential.delete")
    assert (target_id, detail) == (first["header"]["id"], {"kind": "github", "version": 1, "owner_kind": "user"})
    assert len(_hints(published)) == 2
    # the endpoint from before creates a new primary record with a new ID; the second record keeps its place
    fresh = studio.desk.record(writer)
    assert studio.desk.put(writer, fresh, 0).json() == {"version": 1}
    assert studio.primary_of(writer) == [fresh["header"]["id"]]
    assert writer.browser.get(f"/v1/github-identities/{second['header']['id']}").json()["primary"] is False
    # deleting through the endpoint from before removes the primary record only
    assert writer.browser.delete("/v1/me/credentials/github").status_code == 200
    assert writer.browser.get(f"/v1/github-identities/{second['header']['id']}").status_code == 200


def test_a_new_record_after_the_primary_was_deleted_becomes_primary(studio: Studio) -> None:
    writer = studio.scene.writer
    first = studio.created(writer)
    assert writer.browser.delete(f"/v1/github-identities/{first['header']['id']}").status_code == 200
    second = studio.created(writer)
    assert studio.primary_of(writer) == [second["header"]["id"]]


def test_reads_and_deletes_of_invisible_records_are_not_found(studio: Studio) -> None:
    writer, stranger = studio.scene.writer, studio.scene.stranger
    foreign = studio.created(stranger)
    record_id = foreign["header"]["id"]
    for response in (
        writer.browser.get(f"/v1/github-identities/{record_id}"),
        writer.browser.get(f"/v1/github-identities/{new_id()}"),
        writer.browser.get("/v1/github-identities/not-a-uuid", extensions=NO_CONTRACT),
        writer.browser.delete(f"/v1/github-identities/{record_id}"),
    ):
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    legacy = studio.desk.record(writer, "mailbox")
    assert studio.desk.put(writer, legacy, 0).status_code == 200
    response = writer.browser.get(f"/v1/github-identities/{legacy['header']['id']}")
    assert response.status_code == 404
    assert studio.rows("SELECT count(*) FROM hub.credential WHERE owner_id = %s", stranger.id) == [(1,)]


def test_the_rotation_may_leave_non_primary_records_out(studio: Studio) -> None:
    writer = studio.scene.writer
    primary, other = studio.created(writer), studio.created(writer)
    rotated = studio.personal(writer, record_id=primary["header"]["id"], version=2, epoch=2)
    wraps = [studio.desk.wrap(writer, writer, 2, writer)]
    response = writer.browser.post("/v1/me/credentials/rotate", json={"epoch": 2, "wraps": wraps, "records": [rotated]})
    assert response.status_code == 200, response.text
    epochs = dict(studio.rows("SELECT id::text, epoch FROM hub.credential WHERE owner_id = %s", writer.id))
    assert epochs == {primary["header"]["id"]: 2, other["header"]["id"]: 1}
    # a current client re-seals the record that was left out under the new epoch
    resealed = studio.personal(writer, record_id=other["header"]["id"], version=2, epoch=2)
    assert studio.put(writer, resealed, 1).json() == {"version": 2}


def test_the_rotation_may_include_non_primary_records(studio: Studio) -> None:
    writer = studio.scene.writer
    primary, other = studio.created(writer), studio.created(writer)
    records = [
        studio.personal(writer, record_id=primary["header"]["id"], version=2, epoch=2),
        studio.personal(writer, record_id=other["header"]["id"], version=2, epoch=2),
    ]
    wraps = [studio.desk.wrap(writer, writer, 2, writer)]
    response = writer.browser.post("/v1/me/credentials/rotate", json={"epoch": 2, "wraps": wraps, "records": records})
    assert response.status_code == 200, response.text
    assert studio.rows("SELECT DISTINCT epoch FROM hub.credential WHERE owner_id = %s", writer.id) == [(2,)]


def test_the_rotation_still_needs_the_primary_record(studio: Studio) -> None:
    writer = studio.scene.writer
    studio.created(writer)
    other = studio.created(writer)
    records = [studio.personal(writer, record_id=other["header"]["id"], version=2, epoch=2)]
    wraps = [studio.desk.wrap(writer, writer, 2, writer)]
    response = writer.browser.post("/v1/me/credentials/rotate", json={"epoch": 2, "wraps": wraps, "records": records})
    assert (response.status_code, response.json()) == (409, {"error": "version_conflict"})
    stale = [studio.personal(writer, record_id=other["header"]["id"], version=3, epoch=2)]
    response = writer.browser.post("/v1/me/credentials/rotate", json={"epoch": 2, "wraps": wraps, "records": stale})
    assert (response.status_code, response.json()) == (409, {"error": "version_conflict"})


def test_a_record_becomes_primary_when_there_is_none(studio: Studio) -> None:
    writer = studio.scene.writer
    first, second = studio.created(writer), studio.created(writer)
    assert writer.browser.delete(f"/v1/github-identities/{first['header']['id']}").status_code == 200
    response = writer.browser.put(f"/v1/github-identities/{second['header']['id']}/primary", json={})
    assert response.status_code == 200
    assert studio.primary_of(writer) == [second["header"]["id"]]
    assert writer.browser.get("/v1/me/credentials/github").json() == {"record": second}


def test_a_record_deleted_meanwhile_is_not_found(studio: Studio) -> None:
    writer = studio.scene.writer
    record = studio.created(writer)
    with studio.hub.pool.connection() as connection:
        identity = records.load_identity(connection, UUID(record["header"]["id"]))
    assert identity is not None
    assert writer.browser.delete(f"/v1/github-identities/{record['header']['id']}").status_code == 200
    actor = records.Author(id=UUID(writer.id), kind="user", sig_public=b"")
    with studio.hub.pool.connection() as connection, pytest.raises(HubError, match="not_found"):
        records.delete_identity(
            connection, identity, actor=actor, now=studio.hub.clock(), root_key=studio.hub.config.platform_root_key
        )
