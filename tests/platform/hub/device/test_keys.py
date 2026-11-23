"""U-HUB-device-api (gate): the device resolver and the rights `device:any` / `device` (03-api.md sections 1, 3)."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

from agent_hub.auth.keys import Principal
from agent_hub.device import keys
from agent_hub.errors import HubError
from agent_hub.http import limits
from agent_hub.http.pipeline import key_kind
from agent_hub.http.routing import route_spec
from agent_hub.registry import KEY_RESOLVERS, merge_mappings
from tests.platform.hub.device.support import Fleet, new_device, org_device_scene
from tests.platform.hub.orgs.support import World


def refusal(response: object) -> tuple[int, str]:
    return response.status_code, response.json()["error"]  # type: ignore[attr-defined]


def test_resolver_is_registered() -> None:
    assert merge_mappings(KEY_RESOLVERS)["device."] is keys.resolve_device
    assert keys.key_id_of("01937a3c-0000-7000-8000-000000000001") == "device.01937a3c000070008000000000000001"


def test_pending_device_reads_only_its_state(fleet: Fleet) -> None:
    device = new_device()
    found = fleet.platform_invite()
    assert fleet.enroll_platform(device, found).status_code == 200
    client = fleet.client(device)
    assert client.get("/v1/device/state").json() == {
        "state": "pending_certification",
        "hub_time": "2026-11-21T12:00:00.123Z",
    }
    for path in ("/v1/device/platform-log", "/v1/device/wraps", "/v1/device/pull"):
        assert refusal(client.get(path)) == (403, "forbidden"), path
    assert refusal(client.post("/v1/device/wraps", json={"wraps": [{}]}, extensions={"contract": False})) == (
        403,
        "forbidden",
    )


def test_active_device_passes(fleet: Fleet) -> None:
    device, _ = fleet.platform_device()
    response = fleet.client(device).get("/v1/device/platform-log")
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True


def test_decertified_removed_and_disabled_devices_are_revoked(fleet: Fleet) -> None:
    device, _ = fleet.platform_device()
    client = fleet.client(device)
    fleet.platform.post({"type": "device_decertify", "device": device.id})
    assert refusal(client.get("/v1/device/state")) == (401, "key_revoked")
    other, _ = fleet.platform_device()
    fleet.execute("UPDATE hub.device SET state = 'removed' WHERE id = %s", other.id)
    removed = fleet.client(other).get("/v1/device/state")
    assert (refusal(removed), removed.extensions["hub_signature"]) == ((401, "key_revoked"), True)
    third, _ = fleet.platform_device()
    fleet.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", fleet.hub.clock(), third.id)
    disabled = fleet.client(third).get("/v1/device/state")
    assert (refusal(disabled), disabled.extensions["hub_signature"]) == ((401, "key_revoked"), True)


def test_org_device_removed_from_its_org_is_revoked(world: World, fleet: Fleet) -> None:
    scene = org_device_scene(world)
    client = fleet.client(scene.device)
    assert client.get("/v1/device/state").status_code == 200
    fleet.execute(
        "UPDATE hub.org_member SET removed_seq = 99 WHERE org_id = %s AND principal_id = %s",
        scene.org,
        scene.device.id,
    )
    left = client.get("/v1/device/state")
    assert (refusal(left), left.extensions["hub_signature"]) == ((401, "key_revoked"), True)


def test_refusal_of_a_revoked_device_is_signed_for_its_holder_only(fleet: Fleet) -> None:
    # 03-api.md section 1: every response to a signed request is signed; the runner trusts only a verified
    # `key_revoked` (05-runner.md section 2.3). The gate checks the signature with the revoked key first.
    device, _ = fleet.platform_device()
    fleet.platform.post({"type": "device_decertify", "device": device.id})
    revoked = fleet.client(device).get("/v1/device/state")
    assert refusal(revoked) == (401, "key_revoked")
    assert revoked.extensions["hub_signature"] is True
    forged = fleet.client(device, new_device().identity).get("/v1/device/state")
    assert refusal(forged) == (401, "signature_invalid")
    assert forged.extensions["hub_signature"] is None  # unsigned: the request was not the holder's
    # a verified refusal is no failed signature: 40 of them do not block the source
    for _ in range(40):
        assert refusal(fleet.client(device).get("/v1/device/state")) == (401, "key_revoked")
    other, _ = fleet.platform_device()
    assert fleet.client(other).get("/v1/device/state").status_code == 200


def test_verified_refusals_count_against_the_key(fleet: Fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    # the holder of a revoked key gets no more requests than the key's limit (03-api.md section 1)
    device, _ = fleet.platform_device()
    fleet.platform.post({"type": "device_decertify", "device": device.id})
    monkeypatch.setitem(limits.PER_MINUTE, "device", 3)
    for _ in range(3):
        assert refusal(fleet.client(device).get("/v1/device/state")) == (401, "key_revoked")
    limited = fleet.client(device).get("/v1/device/state")
    assert refusal(limited) == (429, "rate_limited")
    assert limited.extensions["hub_signature"] is True


def test_unknown_device_and_wrong_key(fleet: Fleet) -> None:
    assert refusal(fleet.client(new_device()).get("/v1/device/state")) == (401, "key_unknown")
    device, _ = fleet.platform_device()
    assert refusal(fleet.client(device, new_device().identity).get("/v1/device/state")) == (401, "signature_invalid")


def test_browser_keys_are_no_device(fleet: Fleet, world: World) -> None:
    user = world.user()
    assert refusal(user.browser.get("/v1/device/state")) == (403, "forbidden")
    session = Principal(kind="session", key_id="session." + "0" * 32, principal_id=None, actor_kind="user")
    with pytest.raises(HubError):
        keys.any_device(session)
    with pytest.raises(HubError):
        keys.active_device(session)
    device = Principal(kind="device", key_id="device." + "0" * 32, principal_id=None, actor_kind="device")
    assert keys.any_device(Principal(**{**device.__dict__, "attributes": {"state": keys.PENDING}})).kind == "device"
    with pytest.raises(HubError):
        keys.any_device(device)


def test_every_device_route_requires_an_active_device(fleet: Fleet) -> None:
    """Fail closed for later packages too: every `auth="device"` route except `GET /v1/device/state` depends on
    `ActiveDevice`, so a device waiting for its certification reaches nothing else (04-crypto.md section 5.9)."""
    checked = []
    for route in fleet.hub.app.router.routes:
        spec = route_spec(route)
        if spec is None or spec.auth != "device" or not isinstance(route, APIRoute | APIWebSocketRoute):
            continue
        pending = [route.dependant]
        calls = set()
        while pending:
            dependant = pending.pop()
            calls.add(dependant.call)
            pending.extend(dependant.dependencies)
        expected = keys.any_device if route.path == "/v1/device/state" else keys.active_device
        assert expected in calls, route.path
        checked.append(route.path)
    assert "/v1/device/pull" in checked


def test_rate_limit_class_of_a_key_id() -> None:
    assert [key_kind(k) for k in (f"device.{'0' * 32}", f"session.{'1' * 32}", "platform-root")] == [
        "device",
        "session",
        "root",
    ]
