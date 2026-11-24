"""U-HUB-github (ADR-076 sections 1 and 3, 03-api.md section 6.1): device access to identity records. A device reads
a user's record when it is eligible for the user's `CK`, a company record when it is a reader device of the org
credential key; the read clears its change notice. A device writes only a user's record while answering that user's
`github.connect` (not one for an organization). The endpoints from before answer `404`/`403` for an organization, so
an older runner that follows the notice of a company record is harmless."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from agent_hub.credentials import eligibility
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import SignedClient
from tests.platform.hub.credentials.support import Json
from tests.platform.hub.github.support import NO_CONTRACT, Studio
from tests.platform.hub.orgs.support import Actor, PlatformLog, insert_platform_entries, new_id


def _changed(client: SignedClient) -> list[str]:
    return list(client.get("/v1/device/state").json()["credentials_changed"])


def _request(studio: Studio, user: Actor, device: Actor, *, org_id: str | None = None) -> str:
    envelope = studio.desk.request(user, device)
    body: Json = {"envelope": envelope}
    if org_id is not None:
        body["org_id"] = org_id
    response = user.browser.post("/v1/device-requests", json=body)
    assert response.status_code == 200, response.text
    return str(envelope["header"]["id"])


def _device_put(client: SignedClient, owner: str, record: Json, expected: int, request_id: str, **options: Any) -> Any:
    return client.put(
        f"/v1/device/github-identities/{record['header']['id']}",
        json={"owner_id": owner, "record": record, "expected_version": expected, "request_id": request_id},
        **options,
    )


def test_an_eligible_device_reads_a_users_record_and_clears_its_notice(studio: Studio) -> None:
    writer, device = studio.scene.writer, studio.scene.device
    studio.created(writer)
    second = studio.created(writer)
    client = studio.desk.client(device)
    assert _changed(client) == [writer.id]
    response = client.get(f"/v1/device/github-identities/{second['header']['id']}")
    assert response.json() == {"owner_id": writer.id, "owner_kind": "user", "primary": False, "record": second}
    assert _changed(client) == [writer.id]  # one record read: the notice may stand for the other one as well
    listed = client.get("/v1/device/github-identities", params={"owner_id": writer.id})
    assert len(listed.json()["identities"]) == 2
    assert _changed(client) == []
    assert _changed(studio.desk.client(studio.scene.other_device)) == [writer.id]


def test_a_reader_device_reads_a_company_record(studio: Studio) -> None:
    device = studio.scene.device
    record = studio.created_company()
    client = studio.desk.client(device)
    assert _changed(client) == [studio.org]
    response = client.get(f"/v1/device/github-identities/{record['header']['id']}")
    assert response.json() == {"owner_id": studio.org, "owner_kind": "org", "primary": False, "record": record}
    assert _changed(client) == [studio.org]
    assert client.get("/v1/device/github-identities", params={"owner_id": studio.org}).status_code == 200
    assert _changed(client) == []
    stranger = studio.desk.client(studio.desk.stranger_device())
    refused = stranger.get(f"/v1/device/github-identities/{record['header']['id']}")
    assert (refused.status_code, refused.json()) == (404, {"error": "not_found"})


def test_reads_refused_as_not_found(studio: Studio) -> None:
    stranger = studio.scene.stranger
    foreign = studio.created(stranger)
    client = studio.desk.client(studio.scene.device)
    for url in (
        f"/v1/device/github-identities/{foreign['header']['id']}",
        f"/v1/device/github-identities/{new_id()}",
        "/v1/device/github-identities/not-a-uuid",
    ):
        response = client.get(url, extensions=NO_CONTRACT)
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_the_endpoints_from_before_refuse_an_organization_and_clear_its_notice(studio: Studio) -> None:
    device = studio.scene.device
    studio.created_company()
    client = studio.desk.client(device)
    assert _changed(client) == [studio.org]
    response = client.get(f"/v1/device/credentials/{studio.org}/github")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert _changed(client) == []
    request_id = _request(studio, studio.scene.writer, device)
    put = client.put(
        f"/v1/device/credentials/{studio.org}/github",
        json={"record": studio.company(), "expected_version": 0, "request_id": request_id},
    )
    assert (put.status_code, put.json()) == (403, {"error": "forbidden"})


def test_a_device_writes_a_users_records_while_answering_a_connect(studio: Studio, published: MemoryPublisher) -> None:
    writer, device, other = studio.scene.writer, studio.scene.device, studio.scene.other_device
    request_id = _request(studio, writer, device)
    client = studio.desk.client(device)
    published.clear()
    first = studio.personal(writer, author=device)
    assert _device_put(client, writer.id, first, 0, request_id).json() == {"version": 1}
    second = studio.personal(writer, author=device)
    assert _device_put(client, writer.id, second, 0, request_id).json() == {"version": 1}
    assert studio.primary_of(writer) == [first["header"]["id"]]
    update = studio.personal(writer, record_id=second["header"]["id"], version=2, author=device)
    assert _device_put(client, writer.id, update, 1, request_id).json() == {"version": 2}
    audited = studio.audit("credential.put")
    assert [(row[0], str(row[2]), row[5]["owner_kind"]) for row in audited] == [("device", device.id, "user")] * 3
    hints = {(item.frame, str(item.audience.id)) for item in published.items()}
    assert hints == {("credentials", other.id)}


@pytest.mark.parametrize("case", ["no_request", "org_request", "company_record", "org_owner", "other_device", "bad_id"])
def test_refused_device_writes(studio: Studio, case: str) -> None:
    writer, device = studio.scene.writer, studio.scene.device
    client = studio.desk.client(device)
    record = studio.personal(writer, author=device)
    owner = writer.id
    path_options: dict[str, Any] = {}
    if case == "no_request":
        request_id = new_id()
    elif case == "org_request":
        request_id = _request(studio, studio.promote(writer), device, org_id=studio.org)
    elif case == "company_record":
        company = studio.created_company()
        request_id = _request(studio, writer, device)
        record = studio.personal(writer, record_id=company["header"]["id"], author=device)
    elif case == "org_owner":
        request_id = _request(studio, writer, device)
        record = studio.company(author=device)
        owner = studio.org
    elif case == "other_device":
        request_id = _request(studio, writer, studio.scene.other_device)
    else:
        request_id = _request(studio, writer, device)
        path_options = {"extensions": NO_CONTRACT}
    if case == "bad_id":
        response = client.put(
            "/v1/device/github-identities/not-a-uuid",
            json={"owner_id": owner, "record": record, "expected_version": 0, "request_id": request_id},
            **path_options,
        )
    else:
        response = _device_put(client, owner, record, 0, request_id)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert studio.rows("SELECT count(*) FROM hub.credential WHERE owner_kind = 'user'") == [(0,)]


def test_a_connect_for_an_organization_is_an_admins_request(studio: Studio) -> None:
    reader, device = studio.scene.reader, studio.scene.device
    admin = studio.promote(studio.scene.writer)
    request_id = _request(studio, admin, device, org_id=studio.org)
    assert studio.rows("SELECT org_id::text FROM hub.device_request WHERE id = %s", request_id) == [(studio.org,)]
    envelope = studio.desk.request(reader, device)
    refused = reader.browser.post("/v1/device-requests", json={"envelope": envelope, "org_id": studio.org})
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    login = studio.desk.request(admin, device, "claude.login")
    refused = admin.browser.post("/v1/device-requests", json={"envelope": login, "org_id": studio.org})
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    # a repeat must name the same organization
    envelope = studio.desk.request(admin, device)
    assert (
        admin.browser.post("/v1/device-requests", json={"envelope": envelope, "org_id": studio.org}).status_code == 200
    )
    replayed = admin.browser.post("/v1/device-requests", json={"envelope": envelope, "org_id": studio.org})
    assert replayed.json()["replayed"] is True
    conflict = admin.browser.post("/v1/device-requests", json={"envelope": envelope})
    assert (conflict.status_code, conflict.json()) == (409, {"error": "id_conflict"})


def test_an_eligible_device_lists_the_records_of_an_owner(studio: Studio) -> None:
    writer, device = studio.scene.writer, studio.scene.device
    client = studio.desk.client(device)
    assert client.get("/v1/device/github-identities", params={"owner_id": writer.id}).json() == {"identities": []}
    first = studio.created(writer)
    studio.hub.clock.advance(1)
    second = studio.created(writer)
    company = studio.created_company()
    listed = client.get("/v1/device/github-identities", params={"owner_id": writer.id}).json()["identities"]
    assert [(item["id"], item["primary"], item["owner_kind"]) for item in listed] == [
        (second["header"]["id"], False, "user"),
        (first["header"]["id"], True, "user"),
    ]
    listed = client.get("/v1/device/github-identities", params={"owner_id": studio.org}).json()["identities"]
    assert [(item["id"], item["owner_id"], item["owner_kind"]) for item in listed] == [
        (company["header"]["id"], studio.org, "org")
    ]
    assert studio.rows("SELECT count(*) FROM hub.audit WHERE action = 'read.device.github_identities'") != [(0,)]


def test_the_device_list_is_refused_without_eligibility(studio: Studio) -> None:
    writer, stranger = studio.scene.writer, studio.scene.stranger
    studio.created(writer)
    studio.created(stranger)
    studio.created_company()
    client = studio.desk.client(studio.scene.device)
    foreign = studio.desk.client(studio.desk.stranger_device())
    for device_client, query in (
        (client, {"owner_id": stranger.id}),  # a user the device is not eligible for
        (foreign, {"owner_id": writer.id}),  # a device of another org
        (foreign, {"owner_id": studio.org}),  # an org the device is no reader of
        (client, {"owner_id": studio.desk.world.public_org().id}),  # another org
        (client, {"owner_id": new_id()}),
        (client, {"owner_id": "not-a-uuid"}),
        (client, {}),
    ):
        response = device_client.get("/v1/device/github-identities", params=query, extensions=NO_CONTRACT)
        assert (response.status_code, response.json()) == (404, {"error": "not_found"}), query


def test_a_decertified_platform_device_is_no_reader_of_company_records(studio: Studio) -> None:
    world, hub = studio.desk.world, studio.hub
    device = world.device("platform")
    log = PlatformLog()
    certified = log.certify(device)
    insert_platform_entries(hub.pool, log.entries, hub.clock())
    private = world.private_org()
    admin = private.founder
    private.add("device_assign", admin, device=device.id, wg=private.default_wg, platform_seq=certified)
    wraps = private.wraps(admin, "wg", private.default_wg, 1, [device]) + private.wraps(
        admin, "org", private.id, 1, [device]
    )
    assert private.post(admin, wraps).status_code == 200
    root_key = hub.config.platform_root_key
    with hub.pool.connection() as connection:
        assert eligibility.org_device_ids(connection, private.id, root_key) == {UUID(device.id)}
        assert eligibility.holds(connection, device.id, private.id, root_key)
    # the platform log ends the certification; the device row itself still says active (KM-35: the reader rule)
    log.add("device_decertify", device=device.id)
    insert_platform_entries(hub.pool, log.entries[-1:], hub.clock(), first=len(log.entries) - 1)
    assert world.rows("SELECT state FROM hub.device WHERE id = %s", device.id) == [("active",)]
    with hub.pool.connection() as connection:
        assert eligibility.org_device_ids(connection, private.id, root_key) == set()
        assert not eligibility.holds(connection, device.id, private.id, root_key)
        assert eligibility.holder_ids(connection, private.id, root_key) == set()
