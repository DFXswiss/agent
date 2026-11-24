"""U-HUB-device-api (enrollment): `POST /v1/device/enroll` for org and platform devices (03-api.md section 6.1,
04-crypto.md sections 3, 5.8, 5.9)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_hub.crypto import invite as invite_format
from agent_hub.crypto.keys import Identity
from agent_hub.device import enroll
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.device.support import Fleet, new_device
from tests.platform.hub.orgs.support import Actor, World, new_id

NOT_CONTRACT = {"contract": False}


def error(response: Any) -> str:
    return str(response.json()["error"])


def audit(fleet: Fleet, action: str) -> list[tuple[Any, ...]]:
    return fleet.rows(
        "SELECT actor_kind, actor_id::text, org_id::text, device_id::text, target_kind, target_id, detail"
        " FROM hub.audit WHERE action = %s ORDER BY seq",
        action,
    )


# --- platform devices ---


def test_platform_enrollment_waits_for_certification(fleet: Fleet) -> None:
    device = new_device()
    found = fleet.platform_invite()
    response = fleet.enroll_platform(device, found)
    assert response.status_code == 200
    assert response.json() == {"state": "pending_certification"}
    assert response.extensions["hub_signature"] is True
    rows = fleet.rows("SELECT kind, state, owner_org, os, arch, capacity FROM hub.device WHERE id = %s", device.id)
    assert rows == [("platform", "pending_certification", None, "linux", "arm64", rows[0][5])]
    stored = fleet.rows(
        "SELECT invite_id, request, certified_at FROM hub.device_enrollment WHERE device_id = %s", device.id
    )
    assert stored == [(found.invite_id, fleet.platform_enroll_body(device, found), None)]
    assert audit(fleet, "device.enroll") == [
        (
            "device",
            device.id,
            None,
            device.id,
            "invite",
            found.invite_id,
            {"device_kind": "platform", "state": "pending_certification"},
        )
    ]
    listed = fleet.hub.root.get("/v1/platform/enrollments").json()["enrollments"]
    assert [item["device_id"] for item in listed] == [device.id]
    assert fleet.client(device).get("/v1/device/state").json()["state"] == "pending_certification"
    fleet.certify(device, found)
    assert fleet.client(device).get("/v1/device/state").json()["state"] == "active"
    assert fleet.rows("SELECT certified_at IS NOT NULL FROM hub.device_enrollment") == [(True,)]


def test_platform_enrollment_replay_returns_the_original_answer(fleet: Fleet) -> None:
    device = new_device()
    found = fleet.platform_invite()
    assert fleet.enroll_platform(device, found).status_code == 200
    again = fleet.enroll_platform(device, found)
    assert again.status_code == 200
    assert again.json() == {"state": "pending_certification"}
    assert fleet.rows("SELECT count(*) FROM hub.device_enrollment") == [(1,)]
    assert len(audit(fleet, "device.enroll")) == 1


def test_platform_code_is_used_once(fleet: Fleet) -> None:
    found = fleet.platform_invite()
    assert fleet.enroll_platform(new_device(), found).status_code == 200
    second = fleet.enroll_platform(new_device(), found)
    assert second.status_code == 404
    assert error(second) == "invite_unknown"


def test_platform_code_after_certification_is_unknown(fleet: Fleet) -> None:
    device = new_device()
    found = fleet.platform_invite()
    assert fleet.enroll_platform(device, found).status_code == 200
    fleet.certify(device, found)
    assert error(fleet.enroll_platform(new_device(), found)) == "invite_unknown"


def test_unknown_expired_revoked_and_wrong_kind_codes_are_unknown(fleet: Fleet, world: World) -> None:
    unknown = invite_format.invite_secrets(invite_format.generate_code())
    fleet.platform.ensure_root()
    assert error(fleet.enroll_platform(new_device(), unknown)) == "invite_unknown"
    expired = fleet.platform_invite(hours=1)
    fleet.hub.clock.advance(3601)
    assert error(fleet.enroll_platform(new_device(), expired)) == "invite_unknown"
    revoked = fleet.platform_invite()
    fleet.execute("UPDATE hub.invite SET revoked_at = %s WHERE id = %s", fleet.hub.clock(), revoked.invite_id)
    assert error(fleet.enroll_platform(new_device(), revoked)) == "invite_unknown"
    draft = world.public_org()
    org_code = fleet.org_invite(draft)
    assert error(fleet.enroll_platform(new_device(), org_code)) == "invite_unknown"
    device = new_device()
    org_body = fleet.org_enroll_body(draft, device, unknown)
    assert error(fleet.client(device).post("/v1/device/enroll", json=org_body)) == "invite_unknown"


def test_platform_invitation_row_without_log_entry_is_unknown(fleet: Fleet) -> None:
    found = invite_format.invite_secrets(invite_format.generate_code())
    fleet.execute(
        "INSERT INTO hub.invite (id, scope, kind, created_at, expires_at) VALUES (%s, 'platform', 'device', %s, %s)",
        found.invite_id,
        fleet.hub.clock(),
        fleet.hub.clock() + timedelta(hours=1),
    )
    assert error(fleet.enroll_platform(new_device(), found)) == "invite_unknown"
    fleet.platform.ensure_root()
    assert error(fleet.enroll_platform(new_device(), found)) == "invite_unknown"


def test_platform_enrollment_signatures(fleet: Fleet) -> None:
    found = fleet.platform_invite()
    device = new_device()
    wrong_invite = fleet.platform_enroll_body(device, found, invite_key=Ed25519PrivateKey.generate())
    response = fleet.client(device).post("/v1/device/enroll", json=wrong_invite)
    assert (response.status_code, error(response)) == (401, "signature_invalid")
    wrong_device = fleet.platform_enroll_body(device, found, device_key=Ed25519PrivateKey.generate())
    assert error(fleet.client(device).post("/v1/device/enroll", json=wrong_device)) == "signature_invalid"
    assert fleet.rows("SELECT count(*) FROM hub.device") == [(0,)]
    assert fleet.enroll_platform(device, found).status_code == 200


def test_request_signature_proves_possession(fleet: Fleet) -> None:
    found = fleet.platform_invite()
    device = new_device()
    body = fleet.platform_enroll_body(device, found)
    other_key = fleet.client(device, Identity.generate()).post("/v1/device/enroll", json=body)
    assert (other_key.status_code, error(other_key)) == (401, "signature_invalid")
    other_id = fleet.client(new_device(), device.identity).post("/v1/device/enroll", json=body)
    assert (other_id.status_code, error(other_id)) == (401, "signature_missing")
    unsigned = fleet.hub.anon.post("/v1/device/enroll", json=body)
    assert (unsigned.status_code, error(unsigned)) == (401, "signature_missing")
    assert fleet.rows("SELECT count(*) FROM hub.device") == [(0,)]


def test_known_device_id_is_a_conflict(fleet: Fleet, world: World) -> None:
    device = new_device()
    found = fleet.platform_invite()
    assert fleet.enroll_platform(device, found).status_code == 200
    changed = fleet.platform_enroll_body(device, found) | {"os": "macos"}
    response = fleet.client(device).post("/v1/device/enroll", json=changed)
    assert (response.status_code, error(response)) == (409, "id_conflict")
    second = fleet.platform_invite()
    assert error(fleet.enroll_platform(device, second)) == "id_conflict"
    user = world.user()
    as_user = Actor(user.id, "device", user.identity)
    assert error(fleet.enroll_platform(as_user, fleet.platform_invite())) == "id_conflict"
    orphan = new_device()
    fleet.execute(
        "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s)", orphan.id, fleet.hub.clock()
    )
    assert error(fleet.enroll_platform(orphan, fleet.platform_invite())) == "id_conflict"


def test_form_of_each_kind(fleet: Fleet, world: World) -> None:
    found = fleet.platform_invite()
    device = new_device()
    body = fleet.platform_enroll_body(device, found)
    draft = world.public_org()
    entry = fleet.org_enroll_body(draft, device, fleet.org_invite(draft))["entry"]
    with_entry = fleet.client(device).post("/v1/device/enroll", json={**body, "entry": entry}, extensions=NOT_CONTRACT)
    assert with_entry.json() == {"error": "request_invalid", "detail": {"pointers": ["/entry"]}}
    org_body = {key: value for key, value in body.items() if key not in ("enroll_sig", "inv_sig")} | {"kind": "org"}
    no_entry = fleet.client(device).post("/v1/device/enroll", json=org_body, extensions=NOT_CONTRACT)
    assert no_entry.json() == {"error": "request_invalid", "detail": {"pointers": ["/entry"]}}
    sigs = fleet.client(device).post(
        "/v1/device/enroll", json={**org_body, "entry": entry, "inv_sig": body["inv_sig"]}, extensions=NOT_CONTRACT
    )
    assert sigs.json() == {"error": "request_invalid", "detail": {"pointers": ["/inv_sig"]}}
    missing = {key: value for key, value in body.items() if key != "enroll_sig"}
    response = fleet.client(device).post("/v1/device/enroll", json=missing, extensions=NOT_CONTRACT)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/enroll_sig"]}}
    extra = fleet.client(device).post("/v1/device/enroll", json={**body, "x": 1}, extensions=NOT_CONTRACT)
    assert error(extra) == "request_invalid"
    assert fleet.rows("SELECT count(*) FROM hub.device") == [(0,)]


# --- org devices ---


def test_org_enrollment_appends_the_accept(fleet: Fleet, world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    wg = new_id()
    draft.add("wg_create", draft.founder, wg=wg, epoch=1)
    assert draft.post(draft.founder, draft.wraps(draft.founder, "wg", wg, 1, [draft.founder])).status_code == 200
    found = fleet.org_invite(draft, {wg: "device"})
    device = new_device()
    published.clear()
    response = fleet.enroll_org(draft, device, found)
    assert response.status_code == 200, response.text
    assert response.json() == {"state": "active", "org_id": draft.id}
    assert fleet.rows("SELECT kind, state, owner_org::text FROM hub.device WHERE id = %s", device.id) == [
        ("org", "active", draft.id)
    ]
    assert fleet.rows("SELECT role FROM hub.org_member WHERE principal_id = %s", device.id) == [("device",)]
    assert fleet.rows("SELECT wg_id::text FROM hub.wg_device WHERE device_id = %s", device.id) == [(wg,)]
    assert fleet.rows("SELECT used_by::text, package_ct FROM hub.invite WHERE id = %s", found.invite_id) == [
        (device.id, None)
    ]
    assert fleet.rows("SELECT log_head_seq FROM hub.org WHERE id = %s", draft.id) == [(len(draft.entries) - 1,)]
    assert audit(fleet, "device.enroll") == [
        ("device", device.id, draft.id, device.id, "invite", found.invite_id, {"device_kind": "org", "state": "active"})
    ]
    assert [row[1] for row in audit(fleet, "log.accept")][-1] == device.id
    assert audit(fleet, "invite.use") == [
        (
            "device",
            device.id,
            draft.id,
            device.id,
            "invite",
            found.invite_id,
            {"invite_id": found.invite_id, "kind": "device"},
        )
    ]
    frames = [(item.audience, item.frame) for item in published.items()]
    assert (Audience("org", uuid.UUID(draft.id)), "log") in frames
    state = fleet.client(device).get("/v1/device/state").json()
    assert state["assigned"] == [{"org_id": draft.id, "wg_id": wg}]


def test_org_enrollment_replay_and_single_use(fleet: Fleet, world: World) -> None:
    draft = world.public_org()
    found = fleet.org_invite(draft)
    device = new_device()
    body = fleet.org_enroll_body(draft, device, found)
    assert fleet.client(device).post("/v1/device/enroll", json=body).status_code == 200
    again = fleet.client(device).post("/v1/device/enroll", json=body)
    assert again.status_code == 200
    assert again.json() == {"state": "active", "org_id": draft.id}
    draft.entries.append(body["entry"])
    draft.submitted = len(draft.entries)
    other = fleet.enroll_org(draft, new_device(), found)
    assert (other.status_code, error(other)) == (404, "invite_unknown")


def test_org_enrollment_conflicts_for_a_known_device(fleet: Fleet, world: World) -> None:
    draft = world.public_org()
    device = fleet.org_device(draft)
    changed = fleet.org_enroll_body(draft, device, fleet.org_invite(draft))
    assert error(fleet.client(device).post("/v1/device/enroll", json=changed)) == "id_conflict"
    platform = new_device()
    found = fleet.platform_invite()
    assert fleet.enroll_platform(platform, found).status_code == 200
    as_org = fleet.org_enroll_body(draft, platform, fleet.org_invite(draft))
    assert error(fleet.client(platform).post("/v1/device/enroll", json=as_org)) == "id_conflict"
    rekeyed = Actor(device.id, "device", Identity.generate())
    rekeyed_body = fleet.org_enroll_body(draft, rekeyed, fleet.org_invite(draft))
    assert error(fleet.client(rekeyed).post("/v1/device/enroll", json=rekeyed_body)) == "id_conflict"


def test_org_enrollment_refusals(fleet: Fleet, world: World) -> None:
    draft = world.public_org()
    found = fleet.org_invite(draft)
    device = new_device()
    for overrides, pointer in (
        ({"accept": {"subject": new_id()}}, "/entry/body/subject"),
        ({"accept": {"kind": "user"}}, "/entry/body/kind"),
        ({"accept": {"by": new_id()}}, "/entry/body/by"),
        ({"accept": {"invite_id": fleet.platform_invite().invite_id}}, "/entry/body/invite_id"),
        ({"accept": {"pub": Identity.generate().public()}}, "/entry/body/pub"),
        ({"accept": {"org": new_id()}}, "/entry/body/org"),
        ({"accept": {"seq": "1"}}, "/entry/body/seq"),
    ):
        body = fleet.org_enroll_body(draft, device, found, **overrides)
        response = fleet.client(device).post("/v1/device/enroll", json=body, extensions=NOT_CONTRACT)
        assert response.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}, overrides
    stale = fleet.org_enroll_body(draft, device, found, accept={"seq": 0})
    response = fleet.client(device).post("/v1/device/enroll", json=stale, extensions=NOT_CONTRACT)
    assert (response.status_code, error(response)) == (409, "seq_conflict")
    assert "detail" not in response.json()
    bad_invite_sig = fleet.org_enroll_body(draft, device, found, invite_key=Ed25519PrivateKey.generate())
    response = fleet.client(device).post("/v1/device/enroll", json=bad_invite_sig)
    assert response.status_code == 400
    assert error(response) == "log_accept"
    assert fleet.rows("SELECT count(*) FROM hub.device") == [(0,)]
    assert fleet.rows("SELECT used_at FROM hub.invite WHERE id = %s", found.invite_id) == [(None,)]
    assert fleet.enroll_org(draft, device, found).status_code == 200


def test_org_enrollment_with_an_expired_or_revoked_code(fleet: Fleet, world: World) -> None:
    draft = world.public_org()
    expired = fleet.org_invite(draft, hours=1)
    revoked = fleet.org_invite(draft)
    fleet.execute("UPDATE hub.invite SET revoked_at = %s WHERE id = %s", fleet.hub.clock(), revoked.invite_id)
    fleet.hub.clock.advance(3601)
    assert error(fleet.enroll_org(draft, new_device(), expired)) == "invite_unknown"
    assert error(fleet.enroll_org(draft, new_device(), revoked)) == "invite_unknown"
    user_code = invite_format.invite_secrets(invite_format.generate_code())
    world_user_invite = draft.invite(draft.founder, kind="user")
    draft.append(draft.founder)
    fleet.execute(
        "INSERT INTO hub.invite (id, scope, kind, org_id, created_at, expires_at, package_ct)"
        " VALUES (%s, 'org', 'user', %s, %s, %s, %s)",
        world_user_invite.invite_id,
        draft.id,
        fleet.hub.clock(),
        fleet.hub.clock() + timedelta(hours=1),
        b"x",
    )
    assert error(fleet.enroll_org(draft, new_device(), world_user_invite)) == "invite_unknown"
    assert error(fleet.enroll_org(draft, new_device(), user_code)) == "invite_unknown"


def test_enrollment_body_is_stored_as_sent(fleet: Fleet) -> None:
    device = new_device()
    found = fleet.platform_invite()
    body = fleet.platform_enroll_body(device, found)
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=True)
    response = fleet.client(device).post(
        "/v1/device/enroll", content=raw.encode(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert fleet.rows("SELECT request FROM hub.device_enrollment") == [(body,)]


def test_concurrent_enrollment_of_one_device_id(fleet: Fleet, monkeypatch: pytest.MonkeyPatch) -> None:
    device = new_device()
    assert fleet.enroll_platform(device, fleet.platform_invite()).status_code == 200
    monkeypatch.setattr(enroll, "_known_device", lambda connection, device_id: None)
    response = fleet.enroll_platform(device, fleet.platform_invite())
    assert (response.status_code, error(response)) == (409, "id_conflict")


def test_org_replay_of_a_removed_device_is_a_conflict(fleet: Fleet, world: World) -> None:
    draft = world.public_org()
    found = fleet.org_invite(draft)
    device = new_device()
    body = fleet.org_enroll_body(draft, device, found)
    assert fleet.client(device).post("/v1/device/enroll", json=body).status_code == 200
    fleet.execute("UPDATE hub.org_member SET removed_seq = 99 WHERE principal_id = %s", device.id)
    assert error(fleet.client(device).post("/v1/device/enroll", json=body)) == "id_conflict"
