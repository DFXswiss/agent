"""U-HUB-mailboxes (ADR-099 sections 2 and 3, 03-api.md sections 4.8 and 6.1): several mailboxes per user, written,
read and deleted through `/v1/mailboxes/{id}` (a user's first mailbox becomes primary, the mailbox of the automatic
login the endpoints from before keep addressing), the plaintext mirror of the release (`use {wgs}`, only workgroups
the owner has access to) and the device's read of the mailboxes released to a workgroup (only owners whose `CK` it
holds)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.credentials import records
from agent_hub.crypto.ids import uuid7
from agent_hub.mailboxes import store
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.orgs.support import Actor

KIND = "mailbox"


def _put(
    desk: Desk, owner: Actor, *, version: int = 1, record_id: str | None = None, checked: bool = True, **body: Any
) -> Any:
    record = desk.record(owner, KIND, record_id=record_id or uuid7(), version=version)
    return owner.browser.put(
        f"/v1/mailboxes/{record['header']['id']}",
        json={"record": record, "expected_version": version - 1, **body},
        extensions={"contract": checked},
    )


def _created(desk: Desk, owner: Actor, **body: Any) -> str:
    response = _put(desk, owner, **body)
    assert response.status_code == 200, response.text
    assert response.json() == {"version": 1}
    return str(response.request.url.path.rsplit("/", 1)[1])


def _mirror(desk: Desk, record_id: str) -> list[tuple[Any, ...]]:
    return desk.world.rows("SELECT wgs FROM hub.mailbox_use WHERE credential_id = %s", record_id)


def test_the_first_mailbox_is_primary_and_the_legacy_endpoint_reads_it(desk: Desk) -> None:
    writer = desk.scene.writer
    first = _created(desk, writer)
    second = _created(desk, writer, use={"wgs": [desk.scene.wg]})
    listed = writer.browser.get("/v1/me/mailboxes").json()["mailboxes"]
    assert [(item["id"], item["primary"], item["use"]) for item in listed] == [
        (second, False, {"wgs": [desk.scene.wg]}),
        (first, True, None),
    ]
    assert (listed[0]["owner_id"], listed[0]["version"], listed[0]["epoch"]) == (writer.id, 1, 1)
    legacy = writer.browser.get(f"/v1/me/credentials/{KIND}").json()["record"]
    assert legacy["header"]["id"] == first
    got = writer.browser.get(f"/v1/mailboxes/{second}").json()
    assert (got["owner_id"], got["primary"], got["record"]["header"]["id"]) == (writer.id, False, second)
    # the primary record is changed here too, and the mirror stays without `use`
    assert _put(desk, writer, version=2, record_id=first).json() == {"version": 2}
    assert _put(desk, writer, version=2, record_id=second).json() == {"version": 2}
    assert _mirror(desk, second) == [([desk.scene.wg],)]
    assert desk.audit("credential.put")[-1][5] == {"kind": KIND, "version": 2, "owner_kind": "user"}


def test_the_release_mirror_names_only_workgroups_of_the_owner(desk: Desk) -> None:
    writer, wg = desk.scene.writer, desk.scene.wg
    record_id = _created(desk, writer, use={"wgs": [wg]})
    assert _mirror(desk, record_id) == [([wg],)]
    refused = _put(desk, writer, version=2, record_id=record_id, use={"wgs": [uuid7()]})
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    assert _mirror(desk, record_id) == [([wg],)]
    assert _put(desk, writer, version=2, record_id=record_id, use={"wgs": []}).status_code == 200
    assert _mirror(desk, record_id) == []
    assert _put(desk, writer, version=3, record_id=record_id, use={"wgs": ["eng"]}, checked=False).status_code == 400


def test_other_peoples_records_are_not_found_and_delete_removes_mirror_and_record(desk: Desk) -> None:
    writer, reader = desk.scene.writer, desk.scene.reader
    record_id = _created(desk, writer, use={"wgs": [desk.scene.wg]})
    path = f"/v1/mailboxes/{record_id}"
    assert reader.browser.get(path).status_code == 404
    assert reader.browser.delete(path).status_code == 404
    assert writer.browser.get("/v1/mailboxes/not-a-uuid").status_code == 404
    assert writer.browser.delete(path).json() == {}
    assert writer.browser.get("/v1/me/mailboxes").json() == {"mailboxes": []}
    assert _mirror(desk, record_id) == []
    assert [row[5]["kind"] for row in desk.audit("credential.delete")] == [KIND]


def test_the_number_of_mailboxes_is_bounded(desk: Desk) -> None:
    writer = desk.scene.writer
    with desk.hub.pool.connection() as connection, connection.transaction():
        for index in range(records.MAX_MAILBOXES):
            connection.execute(
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " created_at, is_primary) VALUES (%s, %s, %s, 1, 1, '{}', %s, %s, now(), now(), %s)",
                (uuid7(), writer.id, KIND, bytes(28), bytes(64), index == 0),
            )
    response = _put(desk, writer)
    assert (response.status_code, response.json()) == (400, {"error": "size_exceeded"})


def test_a_device_reads_the_mailboxes_released_to_a_workgroup(desk: Desk) -> None:
    writer, reader, device, wg = desk.scene.writer, desk.scene.reader, desk.scene.device, desk.scene.wg
    client = desk.client(device)
    assert client.get("/v1/device/mailboxes", params={"wg_id": wg}).json() == {"mailboxes": []}
    released = _created(desk, writer, use={"wgs": [wg]})
    _created(desk, writer)  # released nowhere
    theirs = _created(desk, reader, use={"wgs": [wg]})
    found = client.get("/v1/device/mailboxes", params={"wg_id": wg}).json()["mailboxes"]
    assert [(item["id"], item["owner_id"]) for item in found] == [(theirs, reader.id), (released, writer.id)]
    assert found[1]["primary"] is True  # the writer's first mailbox
    assert found[1]["record"] == writer.browser.get(f"/v1/mailboxes/{released}").json()["record"]
    # a device that cannot open the owners' keys reads nothing; the workgroup must be named
    stranger = desk.client(desk.stranger_device())
    assert stranger.get("/v1/device/mailboxes", params={"wg_id": wg}).json() == {"mailboxes": []}
    assert client.get("/v1/device/mailboxes", extensions={"contract": False}).status_code == 404
    assert client.get("/v1/device/mailboxes", params={"wg_id": uuid7()}).json() == {"mailboxes": []}


def test_the_device_list_is_bounded(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    writer, device, wg = desk.scene.writer, desk.scene.device, desk.scene.wg
    for _ in range(2):
        _created(desk, writer, use={"wgs": [wg]})
    monkeypatch.setattr(store, "MAX_RELEASED", 1)
    found: Json = desk.client(device).get("/v1/device/mailboxes", params={"wg_id": wg}).json()
    assert len(found["mailboxes"]) == 1
