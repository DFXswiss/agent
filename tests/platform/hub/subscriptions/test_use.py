"""U-HUB-subscriptions (ADR-090 section 3): the plaintext mirror of a further subscription's use, set with `PUT
/v1/subscriptions/{id}` `use`, and the subscriptions a device is told about in `GET /v1/device/state`
`subscriptions.shared` (an organization it serves, or the operator's pool on a platform device, and only owners whose
`CK` it holds)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_hub.crypto.ids import uuid7
from agent_hub.subscriptions import store
from tests.platform.hub.credentials.support import Desk, Json
from tests.platform.hub.orgs.support import Actor

KIND = "claude_subscription"


def _put(desk: Desk, owner: Actor, *, version: int = 1, record_id: str | None = None, **use: Any) -> str:
    record = desk.record(owner, KIND, record_id=record_id or uuid7(), version=version)
    body: Json = {"record": record, "expected_version": version - 1}
    if use:
        body["use"] = use
    response = owner.browser.put(f"/v1/subscriptions/{record['header']['id']}", json=body)
    assert response.status_code == 200, response.text
    return str(record["header"]["id"])


def _shared(desk: Desk, device: Actor) -> list[Json]:
    return list(desk.client(device).get("/v1/device/state").json()["subscriptions"]["shared"])


def _mirror(desk: Desk, record_id: str) -> list[tuple[Any, ...]]:
    return desk.world.rows("SELECT pool, orgs FROM hub.subscription_use WHERE credential_id = %s", record_id)


def test_the_use_mirror_is_set_kept_and_removed(desk: Desk) -> None:
    writer, org = desk.scene.writer, desk.scene.org
    record_id = _put(desk, writer, pool=False, orgs=[org, org])
    assert _mirror(desk, record_id) == [(False, [org])]
    _put(desk, writer, version=2, record_id=record_id)  # without `use` the mirror stays
    assert _mirror(desk, record_id) == [(False, [org])]
    _put(desk, writer, version=3, record_id=record_id, pool=False, orgs=[])
    assert _mirror(desk, record_id) == []
    _put(desk, writer, version=4, record_id=record_id, pool=True, orgs=[])
    assert writer.browser.delete(f"/v1/subscriptions/{record_id}").status_code == 200
    assert _mirror(desk, record_id) == []


def test_a_device_is_told_about_subscriptions_for_its_organizations(desk: Desk) -> None:
    writer, reader, device = desk.scene.writer, desk.scene.reader, desk.scene.device
    org = desk.scene.org
    assert _shared(desk, device) == []
    named = _put(desk, writer, pool=False, orgs=[org])
    _put(desk, writer, pool=False, orgs=[uuid7()])  # another organization
    pooled = _put(desk, reader, pool=True, orgs=[])
    _put(desk, writer)  # measure only
    assert _shared(desk, device) == [{"subscription_id": named, "owner_id": writer.id}]
    # the operator's pool reaches platform devices only
    with desk.hub.pool.connection() as connection:
        found = store.shared_for(connection, UUID(device.id), [], platform=True)
    assert found == [{"subscription_id": pooled, "owner_id": reader.id}]
    # a device that cannot open the owner's key is not told
    assert _shared(desk, desk.stranger_device()) == []
