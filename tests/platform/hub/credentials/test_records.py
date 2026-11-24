"""U-HUB-credentials (R-4.2, R-4.3, R-9.1): the browser's own credential records (03-api.md section 4.8).

Create, read, update with versions and conflicts, idempotent retries, header and signature checks, the epoch rule,
hard deletion with notices and `credentials` hints to every eligible device.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from agent_hub.crypto.encoding import b64u_decode
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.credentials.support import Desk, database_dump

NO_CONTRACT = {"contract": False}


def _hints(published: MemoryPublisher, frame: str = "credentials") -> list[tuple[str, dict[str, Any]]]:
    return [(str(item.audience.id), dict(item.data)) for item in published.items() if item.frame == frame]


def test_create_read_list_and_audit(desk: Desk, published: MemoryPublisher) -> None:
    writer = desk.scene.writer
    record = desk.record(writer)
    response = desk.put(writer, record, 0)
    assert response.status_code == 200, response.text
    assert response.json() == {"version": 1}
    got = writer.browser.get("/v1/me/credentials/github")
    assert got.status_code == 200
    assert got.json() == {"record": record}
    listed = writer.browser.get("/v1/me/credentials").json()
    assert listed == {
        "credentials": [{"kind": "github", "version": 1, "epoch": 1, "updated_at": "2026-11-21T12:00:00.123Z"}]
    }
    [(actor_kind, actor_id, device_id, target_kind, target_id, detail)] = desk.audit("credential.put")
    assert (actor_kind, str(actor_id), device_id, target_kind) == ("user", writer.id, None, "credential")
    assert target_id == record["header"]["id"]
    assert detail == {"kind": "github", "version": 1, "owner_kind": "user"}
    devices = sorted([desk.scene.device.id, desk.scene.other_device.id])
    assert sorted(_hints(published)) == [(device, {"owner_id": writer.id}) for device in devices]
    notices = desk.world.rows("SELECT device_id::text, owner_id::text FROM hub.credential_notice ORDER BY device_id")
    assert notices == [(device, writer.id) for device in devices]
    assert all(item.audience == Audience("device", item.audience.id) for item in published.items())


def test_update_needs_the_expected_version_and_keeps_the_id(desk: Desk) -> None:
    writer = desk.scene.writer
    first = desk.record(writer)
    assert desk.put(writer, first, 0).status_code == 200
    second = desk.record(writer, version=2, record_id=first["header"]["id"])
    assert desk.put(writer, second, 1).json() == {"version": 2}
    [(record_id, version, epoch, header_text, ct, sig)] = desk.stored(writer)
    assert (str(record_id), version, epoch) == (first["header"]["id"], 2, 1)
    assert json.loads(header_text) == second["header"]
    assert bytes(ct) == b64u_decode(second["ct"])
    assert bytes(sig) == b64u_decode(second["sig"])
    third = desk.record(writer, version=3)
    refused = desk.put(writer, third, 2)
    assert refused.status_code == 400
    assert refused.json() == {"error": "request_invalid", "detail": {"pointers": ["/record/header/id"]}}


def test_version_conflicts(desk: Desk) -> None:
    writer = desk.scene.writer
    first = desk.record(writer)
    assert desk.put(writer, first, 0).status_code == 200
    again = desk.put(writer, desk.record(writer), 0)
    assert again.status_code == 409
    assert again.json() == {"error": "version_conflict", "detail": {"expected": 1}}
    ahead = desk.put(writer, desk.record(writer, version=6, record_id=first["header"]["id"]), 5)
    assert ahead.json() == {"error": "version_conflict", "detail": {"expected": 1}}
    missing = desk.put(desk.scene.reader, desk.record(desk.scene.reader, version=2), 1)
    assert missing.json() == {"error": "version_conflict", "detail": {"expected": 0}}


def test_an_identical_retry_returns_the_stored_version(desk: Desk) -> None:
    writer = desk.scene.writer
    record = desk.record(writer)
    assert desk.put(writer, record, 0).json() == {"version": 1}
    assert desk.put(writer, record, 0).json() == {"version": 1}
    assert len(desk.audit("credential.put")) == 1


def test_the_version_must_follow_the_expected_version(desk: Desk) -> None:
    writer = desk.scene.writer
    response = desk.put(writer, desk.record(writer, version=2), 0)
    assert response.status_code == 400
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/record/header/version"]}}


def test_header_must_name_owner_kind_and_author(desk: Desk) -> None:
    writer, reader = desk.scene.writer, desk.scene.reader
    foreign_owner = desk.record(writer, header={"owner": reader.id})
    assert desk.put(writer, foreign_owner, 0).json() == {"error": "header_invalid"}
    other_kind = desk.record(writer, "mailbox")
    assert desk.put(writer, other_kind, 0, kind="github").json() == {"error": "header_invalid"}
    other_author = desk.record(writer, author=reader)
    assert desk.put(writer, other_author, 0).status_code == 401
    named_other = desk.record(writer, header={"author": reader.id})
    assert desk.put(writer, named_other, 0).json() == {"error": "header_invalid"}
    assert desk.stored(writer) == []


def test_form_and_signature_refusals(desk: Desk) -> None:
    writer = desk.scene.writer
    record = desk.record(writer)
    broken = {"header": record["header"], "ct": record["ct"]}
    response = desk.put(writer, broken, 0, kind="github", extensions=NO_CONTRACT)
    assert response.status_code == 400
    assert response.json() == {"error": "header_invalid"}
    forged = dict(record, sig=desk.record(writer)["sig"])
    response = desk.put(writer, forged, 0)
    assert response.status_code == 401
    assert response.json() == {"error": "signature_invalid"}
    extra = writer.browser.put(
        "/v1/me/credentials/github",
        json={"record": record, "expected_version": 0, "extra": 1},
        extensions=NO_CONTRACT,
    )
    assert extra.status_code == 400
    negative = writer.browser.put(
        "/v1/me/credentials/github", json={"record": record, "expected_version": -1}, extensions=NO_CONTRACT
    )
    assert negative.status_code == 400


def test_the_epoch_must_be_the_current_ck_epoch(desk: Desk) -> None:
    writer = desk.scene.writer
    response = desk.put(writer, desk.record(writer, epoch=2), 0)
    assert response.status_code == 400
    assert response.json() == {"error": "epoch_stale"}
    newcomer = desk.world.user()
    no_ck = newcomer.browser.put(
        "/v1/me/credentials/github", json={"record": desk.record(newcomer), "expected_version": 0}
    )
    assert no_ck.json() == {"error": "epoch_stale"}


def test_a_new_record_with_a_used_id_is_an_id_conflict(desk: Desk) -> None:
    writer, reader = desk.scene.writer, desk.scene.reader
    first = desk.record(writer)
    assert desk.put(writer, first, 0).status_code == 200
    copy = desk.record(reader, record_id=first["header"]["id"])
    response = desk.put(reader, copy, 0)
    assert response.status_code == 409
    assert response.json() == {"error": "id_conflict"}


def test_unknown_kinds_and_missing_records(desk: Desk) -> None:
    writer = desk.scene.writer
    assert writer.browser.get("/v1/me/credentials/github").status_code == 404
    unknown = writer.browser.get("/v1/me/credentials/ssh", extensions=NO_CONTRACT)
    assert unknown.json() == {"error": "not_found"}
    assert writer.browser.get("/v1/me/credentials").json() == {"credentials": []}
    put = desk.put(writer, desk.record(writer), 0, kind="ssh", extensions=NO_CONTRACT)
    assert put.json() == {"error": "not_found"}
    assert writer.browser.delete("/v1/me/credentials/github").json() == {"error": "not_found"}


def test_an_unregistered_user_cannot_write(desk: Desk) -> None:
    user = desk.world.user(registered=False)
    response = user.browser.put("/v1/me/credentials/github", json={"record": desk.record(user), "expected_version": 0})
    assert response.status_code == 403
    assert user.browser.post(
        "/v1/me/credentials/rotate",
        json={"epoch": 1, "wraps": [desk.wrap(user, user, 1, user)], "records": []},
    ).json() == {"error": "forbidden"}


def test_list_is_newest_first(desk: Desk) -> None:
    writer = desk.scene.writer
    assert desk.put(writer, desk.record(writer, "github"), 0).status_code == 200
    desk.hub.clock.advance(1)
    assert desk.put(writer, desk.record(writer, "anthropic_api_key", body={"key": "k"}), 0).status_code == 200
    desk.hub.clock.advance(1)
    assert desk.put(writer, desk.record(writer, "mailbox"), 0).status_code == 200
    kinds = [item["kind"] for item in writer.browser.get("/v1/me/credentials").json()["credentials"]]
    assert kinds == ["mailbox", "anthropic_api_key", "github"]


def test_delete_is_hard_and_notifies_every_eligible_device(desk: Desk, published: MemoryPublisher) -> None:
    writer = desk.scene.writer
    record = desk.record(writer)
    assert desk.put(writer, record, 0).status_code == 200
    with desk.hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.credential_notice")
    published.clear()
    desk.hub.clock.advance(5)
    response = writer.browser.delete("/v1/me/credentials/github")
    assert response.status_code == 200
    assert response.json() == {}
    assert desk.stored(writer) == []
    assert writer.browser.get("/v1/me/credentials/github").status_code == 404
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = desk.audit("credential.delete")
    assert (actor_kind, str(actor_id), target_kind) == ("user", writer.id, "credential")
    assert target_id == record["header"]["id"]
    assert detail == {"kind": "github", "version": 1, "owner_kind": "user"}
    devices = sorted([desk.scene.device.id, desk.scene.other_device.id])
    assert sorted(_hints(published)) == [(device, {"owner_id": writer.id}) for device in devices]
    assert len(desk.world.rows("SELECT 1 FROM hub.credential_notice WHERE owner_id = %s", writer.id)) == 2
    dump = "\n".join(database_dump(desk))
    assert b64u_decode(record["ct"]).hex() not in dump
    assert b64u_decode(record["sig"]).hex() not in dump
    assert record["header"]["salt"] not in dump
    assert writer.browser.delete("/v1/me/credentials/github").status_code == 404


def test_writes_of_a_user_without_eligible_devices_notify_nobody(desk: Desk, published: MemoryPublisher) -> None:
    stranger = desk.scene.stranger
    assert desk.put(stranger, desk.record(stranger), 0).status_code == 200
    assert _hints(published) == []
    assert desk.world.rows("SELECT 1 FROM hub.credential_notice WHERE owner_id = %s", stranger.id) == []
    assert uuid.UUID(stranger.id).version == 7
