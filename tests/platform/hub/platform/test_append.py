"""`POST /v1/platform/log` (03-api.md section 4.3, 04-crypto.md sections 4.8 and 6.2, ADR-023): every entry type valid
and invalid (signature, chain, type rule, unknown root), `expected_seq`, the hub's own rules, the audit rows and the
authentication class. The hub only verifies: nothing it holds can extend the log."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import ed25519_public_raw
from agent_hub.platform import log as platform_log
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.platform.conftest import Platform, new_org, new_user

KEY = b64u_encode(ed25519_public_raw(Ed25519PrivateKey.generate()))


def error(response: Any) -> str:
    return str(response.json()["error"])


def test_root_entry_starts_the_log_and_answers_the_head(plat: Platform) -> None:
    entries = plat.chain(plat.body("root", pub=b64u_encode(ed25519_public_raw(plat.root))))
    response = plat.post(entries)
    assert response.status_code == 200
    head = response.json()["head"]
    assert head["seq"] == 0
    assert plat.query("SELECT seq, type, entry_hash FROM hub.platform_entry") == [(0, "root", head["hash"])]


def test_every_entry_type_is_appended_in_one_batch(plat: Platform, hub: HubHarness) -> None:
    org = new_org(hub)
    user = str(new_user(hub))
    device = plat.enroll()  # root + device_invite appended
    later = plat.chain(
        plat.body("device_certify", **device.certify_fields()),
        plat.body("user_invite", invite_id=b64u_encode(bytes(range(16))), pk_inv=KEY, expires=plat.expires()),
        plat.body("role_grant", subject=user, role="inviter"),
        plat.body("org_platform_access", org=org, granted=True),
        plat.body("role_revoke", subject=user, role="inviter"),
        plat.body("device_decertify", device=device.device_id),
    )
    response = plat.post(later)
    assert response.status_code == 200, response.text
    assert response.json()["head"]["seq"] == 7
    types = [row[0] for row in plat.query("SELECT type FROM hub.platform_entry ORDER BY seq")]
    assert types == [
        "root",
        "device_invite",
        "device_certify",
        "user_invite",
        "role_grant",
        "org_platform_access",
        "role_revoke",
        "device_decertify",
    ]
    subjects = plat.query("SELECT seq, subject_id FROM hub.platform_entry WHERE subject_id IS NOT NULL ORDER BY seq")
    assert [str(subject) for _, subject in subjects] == [device.device_id, user, org, user, device.device_id]
    invite_id = device.invite.invite_id
    assert plat.query(
        "SELECT actor_kind, actor_id, org_id, device_id::text, target_kind, target_id, detail FROM hub.audit"
        " WHERE action = 'invite.use'"
    ) == [("operator", None, None, device.device_id, "invite", invite_id, {"invite_id": invite_id, "kind": "device"})]


def test_root_with_another_key_is_refused_as_genesis(plat: Platform) -> None:
    other = Ed25519PrivateKey.generate()
    entries = plat.chain(plat.body("root", pub=b64u_encode(ed25519_public_raw(other))))
    response = plat.post(entries)
    assert (response.status_code, error(response)) == (400, "log_genesis")
    assert response.json()["detail"] == {"pointers": ["/entries/0"]}


def test_entries_signed_by_another_key_are_refused(plat: Platform, hub: HubHarness) -> None:
    """P4: a hub (or anyone) without the root key cannot add a certification."""
    plat.init()
    hub_key = Ed25519PrivateKey.generate()
    entries = plat.chain(plat.body("role_grant", subject=str(new_user(hub)), role="inviter"), signer=hub_key)
    response = plat.post(entries)
    assert (response.status_code, error(response)) == (400, "log_signature")
    assert plat.query("SELECT count(*) FROM hub.platform_entry") == [(1,)]
    assert plat.query("SELECT count(*) FROM hub.platform_role") == [(0,)]


def test_a_root_signed_log_for_another_root_is_refused_by_the_gate(plat: Platform, hub: HubHarness) -> None:
    """An unknown root: the request itself must be signed by the configured root key."""
    other = Ed25519PrivateKey.generate()
    entries = Platform(hub, root=other).chain(plat.body("root", pub=b64u_encode(ed25519_public_raw(other))))
    response = plat.hub.root.post(
        "/v1/platform/log",
        json={"expected_seq": 0, "entries": entries},
        headers={"Agent-Signature": b64u_encode(other.sign(b"x"))},
    )
    assert (response.status_code, error(response)) == (401, "signature_invalid")


def test_the_chain_is_checked(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    user = str(new_user(hub))
    broken_prev = plat.chain(plat.body("role_grant", subject=user, role="inviter", prev=b64u_encode(bytes(32))))
    assert error(plat.post(broken_prev)) == "log_chain"
    gap = plat.chain(
        plat.body("role_grant", subject=user, role="inviter"),
        plat.body("role_revoke", subject=user, role="inviter", seq=3),
    )
    response = plat.post(gap)
    assert error(response) == "log_chain"
    assert response.json()["detail"] == {"pointers": ["/entries/1"]}
    second_root = plat.chain(plat.body("root", pub=b64u_encode(ed25519_public_raw(plat.root))))
    assert error(plat.post(second_root)) == "log_genesis"


def test_expected_seq_and_the_first_seq_must_follow_the_head(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    body = plat.body("role_grant", subject=str(new_user(hub)), role="inviter")
    response = plat.post(plat.chain(body), expected_seq=5)
    assert (response.status_code, error(response)) == (409, "seq_conflict")
    assert response.json()["detail"] == {"expected_seq": 1}
    wrong_first = plat.chain({**body, "seq": 2})
    response = plat.post(wrong_first)
    assert (response.status_code, error(response)) == (409, "seq_conflict")
    first = plat.chain(plat.body("root", pub=b64u_encode(ed25519_public_raw(plat.root))))
    response = plat.post(first, expected_seq=0)
    assert response.json()["detail"] == {"expected_seq": 1}


def test_an_empty_batch_and_malformed_bodies_are_refused(plat: Platform) -> None:
    contract_off: dict[str, Any] = {"extensions": {"contract": False}}
    response = plat.post([], **contract_off)
    assert (response.status_code, error(response)) == (400, "log_empty")
    response = plat.hub.root.post("/v1/platform/log", json={"expected_seq": "0", "entries": []}, **contract_off)
    assert (response.status_code, error(response)) == (400, "request_invalid")
    response = plat.hub.root.post("/v1/platform/log", json={"expected_seq": 0, "entries": [], "x": 1}, **contract_off)
    assert error(response) == "request_invalid"
    root = plat.body("root", pub=b64u_encode(ed25519_public_raw(plat.root)), extra=1)
    response = plat.post(plat.chain(root), **contract_off)
    assert error(response) == "header_invalid"
    unknown = plat.chain(plat.body("device_wipe", device=str(uuid7())))
    assert error(plat.post(unknown, **contract_off)) == "log_type"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("certify_unknown_invite", "invite_not_in_log"),
        ("certify_with_user_invite", "invite_not_in_log"),
        ("certify_twice", "invite_consumed"),
        ("certify_swapped_key", "log_accept"),
        ("certify_certified_device", "log_rights"),
        ("decertify_unknown", "log_rights"),
        ("decertify_twice", "log_rights"),
        ("role_grant_twice", "log_rights"),
        ("role_revoke_not_held", "log_rights"),
        ("invite_id_reused", "log_rights"),
    ],
)
def test_type_rules(plat: Platform, hub: HubHarness, label: str, code: str) -> None:
    device = plat.enroll()
    user = str(new_user(hub))
    bodies: list[dict[str, object]]
    if label == "certify_unknown_invite":
        bodies = [plat.body("device_certify", **{**device.certify_fields(), "invite_id": b64u_encode(bytes(16))})]
    elif label == "certify_with_user_invite":
        invite_id = plat.invite_user()
        bodies = [plat.body("device_certify", **{**device.certify_fields(), "invite_id": invite_id})]
    elif label == "certify_twice":
        plat.certify(device)
        bodies = [plat.body("device_certify", **{**device.certify_fields(), "device": str(uuid7())})]
    elif label == "certify_swapped_key":
        other = plat.enroll(store=False)
        bodies = [plat.body("device_certify", **{**device.certify_fields(), "pub": other.pub})]
    elif label == "certify_certified_device":
        plat.certify(device)
        second = plat.enroll(device_id=device.device_id, store=False)
        bodies = [plat.body("device_certify", **second.certify_fields())]
    elif label == "decertify_unknown":
        bodies = [plat.body("device_decertify", device=str(uuid7()))]
    elif label == "decertify_twice":
        plat.certify(device)
        plat.append("device_decertify", device=device.device_id)
        bodies = [plat.body("device_decertify", device=device.device_id)]
    elif label == "role_grant_twice":
        plat.append("role_grant", subject=user, role="inviter")
        bodies = [plat.body("role_grant", subject=user, role="inviter")]
    elif label == "role_revoke_not_held":
        bodies = [plat.body("role_revoke", subject=user, role="inviter")]
    else:
        bodies = [plat.body("user_invite", invite_id=device.invite.invite_id, pk_inv=KEY, expires=plat.expires())]
    before = plat.query("SELECT count(*) FROM hub.platform_entry")
    response = plat.post(plat.chain(*bodies))
    assert (response.status_code, error(response)) == (400, code)
    assert plat.query("SELECT count(*) FROM hub.platform_entry") == before


def test_a_failing_entry_rolls_back_the_whole_batch(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    user = str(new_user(hub))
    response = plat.post(
        plat.chain(
            plat.body("role_grant", subject=user, role="inviter"),
            plat.body("role_grant", subject=user, role="inviter"),
        )
    )
    assert error(response) == "log_rights"
    assert response.json()["detail"] == {"pointers": ["/entries/1"]}
    assert plat.query("SELECT count(*) FROM hub.platform_role") == [(0,)]
    assert plat.query("SELECT count(*) FROM hub.audit WHERE action LIKE 'platform.%%'") == [(1,)]


@pytest.mark.parametrize("hours", [-1, 0, 24 * 30 + 1])
def test_invitation_lifetimes_follow_adr_005(plat: Platform, hours: float) -> None:
    plat.init()
    response = plat.post(
        plat.chain(plat.body("user_invite", invite_id=b64u_encode(bytes(16)), pk_inv=KEY, expires=plat.expires(hours)))
    )
    assert (response.status_code, error(response)) == (400, "request_invalid")
    assert response.json()["detail"] == {"pointers": ["/entries/0/body/expires"]}


def test_an_invitation_id_of_an_org_invitation_is_refused(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    org = new_org(hub)
    invite_id = b64u_encode(bytes([7] * 16))
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.invite (id, scope, kind, org_id, created_at, expires_at)"
            " VALUES (%s, 'org', 'user', %s, %s, %s)",
            (invite_id, org, hub.clock(), hub.clock().replace(year=2027)),
        )
    response = plat.post(
        plat.chain(
            plat.body("device_invite", invite_id=invite_id, pk_inv=b64u_encode(bytes(32)), expires=plat.expires())
        )
    )
    assert (response.status_code, error(response)) == (409, "id_conflict")
    assert response.json()["detail"] == {"pointers": ["/entries/0/body/invite_id"]}


def test_platform_access_needs_a_known_org(plat: Platform) -> None:
    plat.init()
    response = plat.post(plat.chain(plat.body("org_platform_access", org=str(uuid7()), granted=True)))
    assert (response.status_code, error(response)) == (404, "not_found")


@pytest.mark.parametrize("kind", ["user", "org-device"])
def test_a_certified_device_id_must_not_be_another_principal(plat: Platform, hub: HubHarness, kind: str) -> None:
    device = plat.enroll(store=False)
    with hub.pool.connection() as connection:
        if kind == "user":
            connection.execute(
                "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)",
                (device.device_id, hub.clock()),
            )
        else:
            connection.execute(
                "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'device', %s)",
                (device.device_id, hub.clock()),
            )
            connection.execute(
                "INSERT INTO hub.device (id, kind, kem_pub, sig_pub, owner_org, state, enrolled_at, agent_version, os,"
                " arch, updated_at) VALUES (%s, 'org', %s, %s, %s, 'active', %s, '0.1.0', 'macos', 'arm64', %s)",
                (device.device_id, bytes(32), bytes(32), uuid7(), hub.clock(), hub.clock()),
            )
    response = plat.post(plat.chain(plat.body("device_certify", **device.certify_fields())))
    assert (response.status_code, error(response)) == (409, "id_conflict")


def test_appends_are_audited(plat: Platform, hub: HubHarness) -> None:
    device = plat.enroll()
    seq = plat.certify(device)
    rows = plat.query(
        "SELECT action, actor_kind, actor_id, device_id, target_kind, target_id, detail FROM hub.audit"
        " WHERE action LIKE 'platform.%%' OR action = 'device.certify' ORDER BY seq"
    )
    assert [row[0] for row in rows] == [
        "platform.root",
        "platform.device_invite",
        "platform.device_certify",
        "device.certify",
    ]
    assert all(row[1] == "operator" and row[2] is None for row in rows)
    assert rows[2][3:] == (uuid.UUID(device.device_id), "platform_entry", str(seq), {"seq": seq})
    assert rows[3][3:] == (uuid.UUID(device.device_id), "device", device.device_id, {"platform_seq": seq})


def test_only_the_root_may_append(plat: Platform, hub: HubHarness) -> None:
    entries = plat.chain(plat.body("root", pub=b64u_encode(ed25519_public_raw(plat.root))))
    body = {"expected_seq": 0, "entries": entries}
    assert error(hub.browser().post("/v1/platform/log", json=body)) == "forbidden"
    assert error(hub.device().post("/v1/platform/log", json=body)) == "forbidden"
    response = hub.anon.post("/v1/platform/log", json=body)
    assert (response.status_code, error(response)) == (401, "signature_missing")


def test_a_stored_log_that_does_not_verify_stops_appends(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    platform_log.CACHE.clear()
    with hub.pool.connection() as connection, pytest.raises(RuntimeError, match="does not verify"):
        platform_log.load(connection, ed25519_public_raw(Ed25519PrivateKey.generate()))


def test_the_cache_follows_the_stored_head(plat: Platform, hub: HubHarness) -> None:
    plat.init()
    root = ed25519_public_raw(plat.root)
    with hub.pool.connection() as connection:
        first = platform_log.load(connection, root)
        assert first is not None
        assert platform_log.load(connection, root) is first
        platform_log.CACHE.clear()
        again = platform_log.load(connection, root)
        assert again is not None
        assert again is not first
        assert again.state.head == first.state.head
        assert platform_log.state(connection, root) == again.state


def test_an_empty_log_has_no_state(hub: HubHarness) -> None:
    with hub.pool.connection() as connection:
        assert platform_log.state(connection, bytes(32)) is None


def test_client_only_and_unknown_codes_are_mapped() -> None:
    assert platform_log.hub_error(CryptoError("platform_root_mismatch")).code == "log_genesis"
    mapped = platform_log.hub_error(CryptoError("log_rollback"), "/entries/0")
    assert (mapped.code, mapped.detail) == ("request_invalid", {"pointers": ["/entries/0"]})
