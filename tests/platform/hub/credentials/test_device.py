"""U-HUB-credentials (R-4.2, R-9.1, R-5.3): device access to credential records (03-api.md section 6.1) and the
`credentials_changed` section of `GET /v1/device/state` (ADR-035).

- `GET /v1/device/credentials/{owner}/{kind}` only for devices eligible for the owner's CK; the read clears the
  device's change notice of the owner (also on `404` after a deletion);
- `PUT` only while answering an active device request of the owner whose kind produces the credential;
- the notices are listed while the device is eligible and pruned by the job when it is not.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.credentials import jobs, notices
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import SignedClient
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.orgs.support import Actor, new_id

NO_CONTRACT = {"contract": False}


def _request(desk: Desk, user: Actor, device: Actor, kind: str = "github.connect", **options: Any) -> str:
    envelope = desk.request(user, device, kind, **options)
    response = desk.send(user, envelope)
    assert response.status_code == 200, response.text
    return str(envelope["header"]["id"])


def _device_put(client: SignedClient, owner: Actor, record: Json, expected: int, request_id: str) -> Any:
    return client.put(
        f"/v1/device/credentials/{owner.id}/{record['header']['kind']}",
        json={"record": record, "expected_version": expected, "request_id": request_id},
    )


def _changed(client: SignedClient) -> list[str]:
    body = client.get("/v1/device/state").json()
    return list(body["credentials_changed"])


def test_an_eligible_device_reads_and_clears_its_notice(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    record = desk.record(writer)
    assert desk.put(writer, record, 0).status_code == 200
    client = desk.client(device)
    assert _changed(client) == [writer.id]
    response = client.get(f"/v1/device/credentials/{writer.id}/github")
    assert response.status_code == 200
    assert response.json() == {"record": record}
    assert _changed(client) == []
    assert _changed(desk.client(desk.scene.other_device)) == [writer.id]


def test_a_deleted_record_is_404_and_still_clears_the_notice(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    assert desk.put(writer, desk.record(writer), 0).status_code == 200
    assert writer.browser.delete("/v1/me/credentials/github").status_code == 200
    client = desk.client(device)
    assert _changed(client) == [writer.id]
    response = client.get(f"/v1/device/credentials/{writer.id}/github")
    assert response.status_code == 404
    assert _changed(client) == []


def test_a_notice_newer_than_the_read_stays(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    assert desk.put(writer, desk.record(writer), 0).status_code == 200
    earlier = desk.hub.clock()
    desk.hub.clock.advance(3)
    assert writer.browser.delete("/v1/me/credentials/github").status_code == 200
    with desk.hub.pool.connection() as connection, connection.transaction():
        notices.clear(connection, desk.uuid(device), desk.uuid(writer), "github", earlier)
    assert _changed(desk.client(device)) == [writer.id]


@pytest.mark.parametrize("case", ["stranger", "foreign_owner_device", "unknown_owner", "bad_owner", "unknown_kind"])
def test_reads_refused_as_not_found(desk: Desk, case: str) -> None:
    writer, stranger = desk.scene.writer, desk.scene.stranger
    assert desk.put(writer, desk.record(writer), 0).status_code == 200
    assert desk.put(stranger, desk.record(stranger), 0).status_code == 200
    client = desk.client(desk.scene.device)
    url = {
        "stranger": f"/v1/device/credentials/{stranger.id}/github",
        "foreign_owner_device": f"/v1/device/credentials/{writer.id}/github",
        "unknown_owner": f"/v1/device/credentials/{new_id()}/github",
        "bad_owner": "/v1/device/credentials/not-a-uuid/github",
        "unknown_kind": f"/v1/device/credentials/{writer.id}/ssh",
    }[case]
    if case == "foreign_owner_device":
        client = desk.client(desk.stranger_device())
    response = client.get(url, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_a_device_writes_while_answering_a_request(desk: Desk, published: MemoryPublisher) -> None:
    writer, device, other = desk.scene.writer, desk.scene.device, desk.scene.other_device
    request_id = _request(desk, writer, device)
    published.clear()
    record = desk.record(writer, author=device)
    response = _device_put(desk.client(device), writer, record, 0, request_id)
    assert response.status_code == 200, response.text
    assert response.json() == {"version": 1}
    assert writer.browser.get("/v1/me/credentials/github").json() == {"record": record}
    [(actor_kind, actor_id, device_id, *_rest)] = desk.audit("credential.put")
    assert (actor_kind, str(actor_id), str(device_id)) == ("device", device.id, device.id)
    hints = [(item.frame, str(item.audience.id), dict(item.data)) for item in published.items()]
    assert hints == [("credentials", other.id, {"owner_id": writer.id})]
    assert _changed(desk.client(device)) == []
    assert _changed(desk.client(other)) == [writer.id]


def test_claude_login_follow_up_authorizes_the_subscription_write(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    root = _request(desk, writer, device, "claude.login")
    follow_up = _request(desk, writer, device, "claude.login", follows=root)
    record = desk.record(writer, "claude_subscription", author=device)
    assert _device_put(desk.client(device), writer, record, 0, follow_up).json() == {"version": 1}


@pytest.mark.parametrize(
    "case",
    [
        "no_request",
        "other_users_request",
        "other_devices_request",
        "kind_not_produced",
        "check_request",
        "answered",
        "expired",
        "ineligible",
        "unknown_kind",
        "bad_owner",
    ],
)
def test_device_writes_refused(desk: Desk, case: str) -> None:
    writer, reader, device = desk.scene.writer, desk.scene.reader, desk.scene.device
    client = desk.client(device)
    record = desk.record(writer, author=device)
    request_id = new_id()
    owner: Actor | str = writer
    if case == "other_users_request":
        request_id = _request(desk, reader, device)
    elif case == "other_devices_request":
        request_id = _request(desk, writer, desk.scene.other_device)
    elif case == "kind_not_produced":
        request_id = _request(desk, writer, device, "claude.login")
    elif case == "check_request":
        request_id = _request(desk, writer, device, "credential.check")
    elif case == "answered":
        request_id = _request(desk, writer, device)
        answer = desk.response(device, writer, request_id, final=True)
        assert client.post(f"/v1/device/requests/{request_id}/responses", json={"envelope": answer}).status_code == 200
    elif case == "expired":
        request_id = _request(desk, writer, device)
        desk.hub.clock.advance(15 * 60)
        record = desk.record(writer, author=device)
    elif case == "ineligible":
        request_id = _request(desk, writer, device)
        client = desk.client(desk.stranger_device())
    elif case == "unknown_kind":
        request_id = _request(desk, writer, device)
        record = {**record, "header": {**record["header"], "kind": "ssh"}}
    else:
        owner = "not-a-uuid"
    owner_text = owner if isinstance(owner, str) else owner.id
    response = client.put(
        f"/v1/device/credentials/{owner_text}/{record['header']['kind']}",
        json={"record": record, "expected_version": 0, "request_id": request_id},
        extensions=NO_CONTRACT,
    )
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert desk.stored(writer) == []


def test_a_device_record_must_be_signed_and_authored_by_the_device(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    request_id = _request(desk, writer, device)
    client = desk.client(device)
    by_owner = desk.record(writer)
    assert _device_put(client, writer, by_owner, 0, request_id).status_code == 401
    named_owner = desk.record(writer, author=device, header={"author": writer.id})
    assert _device_put(client, writer, named_owner, 0, request_id).json() == {"error": "header_invalid"}


def test_notices_of_ineligible_devices_are_hidden_and_pruned(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    assert desk.put(writer, desk.record(writer), 0).status_code == 200
    client = desk.client(device)
    assert _changed(client) == [writer.id]
    with desk.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.wg_access SET revoked_seq = 999 WHERE user_id = %s", (writer.id,))
    assert _changed(client) == []
    reader = desk.scene.reader
    assert desk.put(reader, desk.record(reader), 0).status_code == 200
    assert len(desk.world.rows("SELECT 1 FROM hub.credential_notice")) == 4
    assert jobs.prune_notices(desk.hub.app.state.hub) == 2
    assert desk.world.rows("SELECT owner_id::text FROM hub.credential_notice") == [(reader.id,), (reader.id,)]
    assert jobs.prune_notices(desk.hub.app.state.hub) == 0
    assert [job.name for job in jobs.JOBS] == ["credentials.prune_notices"]


def test_a_re_read_of_one_kind_never_hides_the_change_of_another(desk: Desk) -> None:
    """Review M4: notices are kept per kind."""
    writer, device = desk.scene.writer, desk.scene.device
    assert desk.put(writer, desk.record(writer, "github"), 0).status_code == 200
    assert desk.put(writer, desk.record(writer, "claude_subscription"), 0).status_code == 200
    client = desk.client(device)
    assert client.get(f"/v1/device/credentials/{writer.id}/github").status_code == 200
    assert _changed(client) == [writer.id]
    assert writer.browser.delete("/v1/me/credentials/claude_subscription").status_code == 200
    assert client.get(f"/v1/device/credentials/{writer.id}/github").status_code == 200
    assert _changed(client) == [writer.id]
    assert client.get(f"/v1/device/credentials/{writer.id}/claude_subscription").status_code == 404
    assert _changed(client) == []


def test_a_rotation_notices_every_rotated_kind(desk: Desk) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    github = desk.record(writer, "github")
    assert desk.put(writer, github, 0).status_code == 200
    with desk.hub.pool.connection() as connection:
        connection.execute("DELETE FROM hub.credential_notice")
    rotated = desk.record(
        writer, "github", version=2, epoch=2, record_id=github["header"]["id"], key=desk.key_only(writer, 2)
    )
    wraps = [desk.wrap(writer, writer, 2, writer)]
    body = {"epoch": 2, "wraps": wraps, "records": [rotated]}
    assert writer.browser.post("/v1/me/credentials/rotate", json=body).status_code == 200
    kinds = desk.world.rows("SELECT DISTINCT kind FROM hub.credential_notice WHERE device_id = %s", device.id)
    assert kinds == [("github",)]


def test_records_larger_than_the_contract_are_refused(desk: Desk) -> None:
    """Review M3: `ct` at most 87382 characters (64 KiB)."""
    writer = desk.scene.writer
    record = desk.record(writer, body={"token": "x" * 70000})
    response = desk.put(writer, record, 0, extensions=NO_CONTRACT)
    assert (response.status_code, response.json()) == (400, {"error": "size_exceeded"})
    assert desk.stored(writer) == []
