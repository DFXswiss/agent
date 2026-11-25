"""U-HUB-subscriptions (ADR-090 sections 1 and 2, 03-api.md sections 4.8 and 6.1): several Claude subscriptions per
user. Further subscriptions are written through `/v1/subscriptions/{id}` (never primary, never the primary record),
read and deleted there like the primary record; a device reads them when it is eligible for the owner's `CK` and
writes them while answering the owner's `claude.login` or to re-seal one under the owner's current epoch."""

from __future__ import annotations

from typing import Any

from agent_hub.credentials import records
from agent_hub.crypto.ids import uuid7
from tests.platform.hub.conftest import SignedClient
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.orgs.support import Actor

KIND = "claude_subscription"
NO_CONTRACT = {"contract": False}


def _record(desk: Desk, owner: Actor, **options: Any) -> Json:
    return desk.record(owner, KIND, record_id=options.pop("record_id", None) or uuid7(), **options)


def _put(owner: Actor, record: Json, expected: int, **options: Any) -> Any:
    return owner.browser.put(
        f"/v1/subscriptions/{record['header']['id']}",
        json={"record": record, "expected_version": expected},
        **options,
    )


def _created(desk: Desk, owner: Actor) -> Json:
    record = _record(desk, owner)
    response = _put(owner, record, 0)
    assert response.status_code == 200, response.text
    assert response.json() == {"version": 1}
    return record


def _request(desk: Desk, user: Actor, device: Actor, kind: str = "claude.login") -> str:
    envelope = desk.request(user, device, kind)
    response = desk.send(user, envelope)
    assert response.status_code == 200, response.text
    return str(envelope["header"]["id"])


def _device_put(client: SignedClient, owner: Actor, record: Json, expected: int, request_id: str | None) -> Any:
    body: Json = {"owner_id": owner.id, "record": record, "expected_version": expected}
    if request_id is not None:
        body["request_id"] = request_id
    return client.put(f"/v1/device/subscriptions/{record['header']['id']}", json=body)


def _changed(client: SignedClient) -> list[str]:
    return list(client.get("/v1/device/state").json()["credentials_changed"])


def test_further_subscriptions_are_never_primary_and_the_legacy_record_stays(desk: Desk) -> None:
    writer = desk.scene.writer
    legacy = desk.record(writer, KIND)
    assert desk.put(writer, legacy, 0).status_code == 200
    first, second = _created(desk, writer), _created(desk, writer)
    listed = writer.browser.get("/v1/me/subscriptions").json()["subscriptions"]
    assert [(item["id"], item["primary"], item["reading"]) for item in listed] == [
        (second["header"]["id"], False, None),
        (first["header"]["id"], False, None),
        (legacy["header"]["id"], True, None),
    ]
    assert (listed[0]["owner_id"], listed[0]["version"], listed[0]["epoch"]) == (writer.id, 1, 1)
    # the endpoints from before still address the primary record only
    assert writer.browser.get(f"/v1/me/credentials/{KIND}").json() == {"record": legacy}
    assert [item["kind"] for item in writer.browser.get("/v1/me/credentials").json()["credentials"]] == [KIND]
    got = writer.browser.get(f"/v1/subscriptions/{first['header']['id']}")
    assert got.json() == {"owner_id": writer.id, "primary": False, "record": first}
    assert desk.audit("credential.put")[-1][5] == {"kind": KIND, "version": 1, "owner_kind": "user"}
    # an update keeps the record; the legacy record is written through its own endpoint only
    newer = _record(desk, writer, record_id=first["header"]["id"], version=2)
    assert _put(writer, newer, 1).json() == {"version": 2}
    again = _record(desk, writer, record_id=legacy["header"]["id"], version=2)
    assert _put(writer, again, 1).status_code == 403
    assert writer.browser.get(f"/v1/subscriptions/{legacy['header']['id']}").json()["primary"] is True


def test_a_user_without_a_primary_record_gets_one_through_the_legacy_endpoint(desk: Desk) -> None:
    writer = desk.scene.writer
    further = _created(desk, writer)
    assert writer.browser.get(f"/v1/me/credentials/{KIND}").status_code == 404
    legacy = desk.record(writer, KIND)
    assert desk.put(writer, legacy, 0).status_code == 200
    rows = desk.world.rows(
        "SELECT id::text, is_primary FROM hub.credential WHERE owner_id = %s ORDER BY is_primary", writer.id
    )
    assert rows == [(further["header"]["id"], False), (legacy["header"]["id"], True)]


def test_other_peoples_and_other_kinds_records_are_not_found(desk: Desk) -> None:
    writer, reader = desk.scene.writer, desk.scene.reader
    record = _created(desk, writer)
    path = f"/v1/subscriptions/{record['header']['id']}"
    assert reader.browser.get(path).status_code == 404
    assert reader.browser.delete(path).status_code == 404
    assert writer.browser.get("/v1/subscriptions/not-a-uuid").status_code == 404
    github = desk.record(writer, "github", record_id=uuid7())
    assert (
        writer.browser.put(
            f"/v1/github-identities/{github['header']['id']}",
            json={"owner_id": writer.id, "record": github, "expected_version": 0},
        ).status_code
        == 200
    )
    assert writer.browser.get(f"/v1/subscriptions/{github['header']['id']}").status_code == 404
    # a subscription under the ID of another kind's record is an ID conflict
    clash = _record(desk, writer, record_id=github["header"]["id"])
    assert _put(writer, clash, 0).status_code == 409
    # a record of another owner or kind in the body is refused
    assert _put(writer, _record(desk, reader), 0).status_code == 400
    wrong_path = writer.browser.put(
        "/v1/subscriptions/not-a-uuid", json={"record": record, "expected_version": 0}, **NO_CONTRACT
    )
    assert wrong_path.status_code == 403


def test_the_number_of_subscriptions_is_bounded(desk: Desk) -> None:
    writer = desk.scene.writer
    with desk.hub.pool.connection() as connection, connection.transaction():
        for _ in range(records.MAX_SUBSCRIPTIONS):
            connection.execute(
                "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
                " created_at, is_primary) VALUES (%s, %s, %s, 1, 1, '{}', '\\x00', '\\x00', now(), now(), false)",
                (uuid7(), writer.id, KIND),
            )
    response = _put(writer, _record(desk, writer), 0)
    assert (response.status_code, response.json()) == (400, {"error": "size_exceeded"})


def test_delete_removes_a_further_or_the_primary_record_and_notices_the_devices(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    legacy = desk.record(writer, KIND)
    assert desk.put(writer, legacy, 0).status_code == 200
    further = _created(desk, writer)
    client = desk.client(device)
    client.get("/v1/device/subscriptions", params={"owner_id": writer.id})
    assert _changed(client) == []
    assert writer.browser.delete(f"/v1/subscriptions/{further['header']['id']}").json() == {}
    assert _changed(client) == [writer.id]
    assert writer.browser.delete(f"/v1/subscriptions/{legacy['header']['id']}").status_code == 200
    assert writer.browser.get(f"/v1/me/credentials/{KIND}").status_code == 404
    assert writer.browser.get("/v1/me/subscriptions").json() == {"subscriptions": []}
    assert [row[5]["kind"] for row in desk.audit("credential.delete")] == [KIND, KIND]


def test_an_eligible_device_lists_and_reads_and_clears_its_notice(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    record = _created(desk, writer)
    client = desk.client(device)
    assert _changed(client) == [writer.id]
    got = client.get(f"/v1/device/subscriptions/{record['header']['id']}")
    assert got.json() == {"owner_id": writer.id, "primary": False, "record": record}
    assert _changed(client) == [writer.id]  # one record read leaves the notice
    listed = client.get("/v1/device/subscriptions", params={"owner_id": writer.id}).json()["subscriptions"]
    assert [item["id"] for item in listed] == [record["header"]["id"]]
    assert _changed(client) == []
    stranger = desk.client(desk.stranger_device())
    assert stranger.get(f"/v1/device/subscriptions/{record['header']['id']}").status_code == 404
    assert stranger.get("/v1/device/subscriptions", params={"owner_id": writer.id}).status_code == 404
    assert client.get("/v1/device/subscriptions", **NO_CONTRACT).status_code == 404
    assert client.get(f"/v1/device/subscriptions/{uuid7()}").status_code == 404


def test_a_device_writes_while_answering_the_owners_login(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    client = desk.client(device)
    request_id = _request(desk, writer, device)
    record = desk.record(writer, KIND, record_id=uuid7(), author=device)
    response = _device_put(client, writer, record, 0, request_id)
    assert response.json() == {"version": 1}
    stored = writer.browser.get(f"/v1/subscriptions/{record['header']['id']}").json()
    assert stored["primary"] is False
    assert desk.audit("credential.put")[-1][:3] == ("device", desk.uuid(device), desk.uuid(device))
    # not for another kind of request, not for the primary record, not for an ineligible device
    other = _request(desk, writer, device, "github.connect")
    assert _device_put(client, writer, desk.record(writer, KIND, author=device), 0, other).status_code == 403
    legacy = desk.record(writer, KIND)
    assert desk.put(writer, legacy, 0).status_code == 200
    replaced = desk.record(writer, KIND, record_id=legacy["header"]["id"], version=2, author=device)
    assert _device_put(client, writer, replaced, 1, _request(desk, writer, device)).status_code == 403
    stranger = desk.stranger_device()
    login = _request(desk, writer, device)
    foreign = desk.record(writer, KIND, author=stranger)
    assert _device_put(desk.client(stranger), writer, foreign, 0, login).status_code == 403
    assert (
        client.put(
            "/v1/device/subscriptions/x", json={"owner_id": writer.id, "record": record, "expected_version": 1}
        ).status_code
        == 403
    )


def test_a_device_reseals_a_record_under_an_older_epoch_only(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    client = desk.client(device)
    record = _created(desk, writer)
    resealed = desk.record(writer, KIND, record_id=record["header"]["id"], version=2, epoch=1, author=device)
    # the stored record is under the current epoch: nothing to re-seal
    assert _device_put(client, writer, resealed, 1, None).status_code == 403
    desk.ck(writer, 2)
    assert _device_put(client, writer, resealed, 1, None).status_code == 409  # still under epoch 1: stale
    current = desk.record(writer, KIND, record_id=record["header"]["id"], version=2, epoch=2, author=device)
    assert _device_put(client, writer, current, 1, None).json() == {"version": 2}
    # now it is under the current epoch, and an unknown or primary record is never re-sealed
    later = desk.record(writer, KIND, record_id=record["header"]["id"], version=3, epoch=2, author=device)
    assert _device_put(client, writer, later, 2, None).status_code == 403
    assert _device_put(client, writer, desk.record(writer, KIND, epoch=2, author=device), 0, None).status_code == 403
    legacy = desk.record(writer, KIND, epoch=2)
    assert desk.put(writer, legacy, 0).status_code == 200
    desk.ck(writer, 3)
    primary = desk.record(writer, KIND, record_id=legacy["header"]["id"], version=2, epoch=3, author=device)
    assert _device_put(client, writer, primary, 1, None).status_code == 403
    # a reseal names the record's owner
    assert _device_put(client, desk.scene.reader, current, 2, None).status_code == 403
