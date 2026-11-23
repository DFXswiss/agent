"""U-HUB-wraps: `GET/POST /v1/wraps` (W3 at the hub) and `GET /v1/orgs/{org}/catch-up` (W2 candidates)
(03-api.md section 4.3, 04-crypto.md sections 4.2, 5.2a, 5.11, 6.3)."""

from __future__ import annotations

from agent_hub.crypto import wrap as wrap_format
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.orgs.support import Actor, OrgDraft, PlatformLog, World, insert_platform_entries, new_id


def _cred_epoch(world: World, user: Actor, epoch: int = 1) -> None:
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, %s, %s)",
            (user.id, epoch, world.hub.clock()),
        )


def _cred_wrap(draft: OrgDraft, wrapper: Actor, owner: Actor, recipient: Actor, epoch: int = 1) -> dict[str, object]:
    sealed = wrap_format.wrap_key(
        recipient.identity.kem_public(),
        scope="cred",
        scope_id=owner.id,
        epoch=epoch,
        recipient=recipient.id,
        key=draft.key("cred", owner.id, epoch),
    )
    signature = wrap_format.sign_wrap(
        wrapper.identity.sig, scope="cred", scope_id=owner.id, epoch=epoch, recipient=recipient.id, wrap=sealed
    )
    return {
        "scope": "cred",
        "scope_id": owner.id,
        "epoch": epoch,
        "recipient_id": recipient.id,
        "wrap": sealed,
        "sig": signature,
    }


def test_member_wraps_for_a_new_member_and_lists_them(world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    admin = draft.founder
    member = world.user()
    world.join(draft, member)
    published.clear()
    wraps = draft.wraps(admin, "org", draft.id, 1, [member]) + draft.wraps(admin, "wg", draft.default_wg, 1, [member])
    response = admin.browser.post("/v1/wraps", json={"wraps": wraps})
    assert response.json() == {"stored": 2, "ignored": 0}
    assert [(item.audience.kind, str(item.audience.id), item.frame) for item in published.items()] == [
        ("user", member.id, "wraps")
    ]
    again = admin.browser.post("/v1/wraps", json={"wraps": wraps[:1]})
    assert again.json() == {"stored": 0, "ignored": 1}
    listed = member.browser.get("/v1/wraps").json()["wraps"]
    assert {(item["scope"], item["scope_id"], item["epoch"], item["wrapped_by"]) for item in listed} == {
        ("org", draft.id, 1, admin.id),
        ("wg", draft.default_wg, 1, admin.id),
    }
    assert listed[0]["wrap"] == wraps[0]["wrap"] or listed[0]["wrap"] == wraps[1]["wrap"]
    only_org = member.browser.get("/v1/wraps", params={"scope": "org", "scope_id": draft.id, "epoch": "1"})
    assert [item["scope"] for item in only_org.json()["wraps"]] == ["org"]
    assert member.browser.get("/v1/wraps", params={"epoch": "2"}).json() == {"wraps": []}
    assert admin.browser.get("/v1/wraps", params={"scope": "cred"}).json() == {"wraps": []}


def test_wrap_query_parameters_are_checked(world: World) -> None:
    user = world.user()
    for params in (
        {"scope": "task"},
        {"scope_id": "x"},
        {"epoch": "0"},
        {"epoch": "01"},
        {"epoch": "x"},
        {"epoch": "²"},
    ):
        response = user.browser.get("/v1/wraps", params=params, extensions={"contract": False})
        assert (response.status_code, response.json()) == (400, {"error": "request_invalid"}), params


def test_wraps_outside_w1_w2_refuse_the_whole_request(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member, stranger = world.user(), world.user()
    world.join(draft, member)
    good = draft.wraps(admin, "org", draft.id, 1, [member])
    cases = [
        draft.wraps(admin, "org", draft.id, 1, [stranger]),
        draft.wraps(admin, "org", draft.id, 2, [member]),
        draft.wraps(admin, "wg", new_id(), 1, [member]),
        draft.wraps(admin, "org", new_id(), 1, [member]),
    ]
    for bad in cases:
        response = admin.browser.post("/v1/wraps", json={"wraps": good + bad})
        assert (response.status_code, response.json()) == (400, {"error": "wrap_unauthorized"})
    forged = draft.wraps(member, "org", draft.id, 1, [member])
    assert admin.browser.post("/v1/wraps", json={"wraps": forged}).json() == {"error": "signature_invalid"}
    assert stranger.browser.post("/v1/wraps", json={"wraps": forged}).json() == {"error": "wrap_unauthorized"}
    malformed = dict(good[0], scope_id="not-a-uuid")
    response = admin.browser.post("/v1/wraps", json={"wraps": [malformed]}, extensions={"contract": False})
    assert response.json() == {"error": "header_invalid"}
    assert world.rows("SELECT count(*) FROM hub.key_wrap WHERE recipient_id = %s", member.id) == [(0,)]


def test_credential_wraps_for_eligible_devices(world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    owner = draft.founder
    device, stranger = world.device(), world.user()
    world.join(draft, device, wgs={draft.default_wg: "device"})
    wrap = _cred_wrap(draft, owner, owner, device)
    assert owner.browser.post("/v1/wraps", json={"wraps": [wrap]}).json() == {"error": "wrap_unauthorized"}
    _cred_epoch(world, owner)
    published.clear()
    assert owner.browser.post("/v1/wraps", json={"wraps": [wrap]}).json() == {"stored": 1, "ignored": 0}
    assert [(item.audience.kind, item.frame) for item in published.items()] == [("device", "wraps")]
    foreign = _cred_wrap(draft, owner, owner, stranger)
    assert owner.browser.post("/v1/wraps", json={"wraps": [foreign]}).json() == {"error": "wrap_unauthorized"}


def test_catch_up_lists_the_missing_wraps_the_caller_may_create(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member, device = world.user(), world.device()
    world.join(draft, member)
    world.join(draft, device, wgs={draft.default_wg: "device"})
    _cred_epoch(world, admin)
    missing = admin.browser.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    expected = [
        {"scope": "cred", "scope_id": admin.id, "epoch": 1, "recipient_id": admin.id},
        {"scope": "cred", "scope_id": admin.id, "epoch": 1, "recipient_id": device.id},
        {"scope": "org", "scope_id": draft.id, "epoch": 1, "recipient_id": device.id},
        {"scope": "org", "scope_id": draft.id, "epoch": 1, "recipient_id": member.id},
        {"scope": "wg", "scope_id": draft.default_wg, "epoch": 1, "recipient_id": device.id},
        {"scope": "wg", "scope_id": draft.default_wg, "epoch": 1, "recipient_id": member.id},
    ]
    assert sorted(missing, key=lambda item: tuple(map(str, item.values()))) == sorted(
        expected, key=lambda item: tuple(map(str, item.values()))
    )
    member_view = member.browser.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    assert {"scope": "org", "scope_id": draft.id, "epoch": 1, "recipient_id": member.id} in member_view
    assert all(item["scope"] != "cred" for item in member_view)
    stranger = world.user()
    response = stranger.browser.get(f"/v1/orgs/{draft.id}/catch-up")
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert stranger.browser.get("/v1/orgs/x/catch-up", extensions={"contract": False}).json() == {"error": "not_found"}


def test_catch_up_skips_decertified_platform_devices(world: World) -> None:
    device = world.device("platform")
    platform = PlatformLog()
    seq = platform.certify(device)
    insert_platform_entries(world.hub.pool, platform.entries, world.hub.clock())
    draft = world.private_org()
    admin = draft.founder
    draft.add("device_assign", admin, device=device.id, wg=draft.default_wg, platform_seq=seq)
    assert draft.post(admin, draft.wraps(admin, "wg", draft.default_wg, 1, [device])).status_code == 200
    _cred_epoch(world, admin)
    missing = admin.browser.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    assert {item["recipient_id"] for item in missing} == {admin.id, device.id}
    platform.add("device_decertify", device=device.id)
    insert_platform_entries(world.hub.pool, platform.entries[-1:], world.hub.clock(), first=len(platform.entries) - 1)
    missing = admin.browser.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    assert {item["recipient_id"] for item in missing} == {admin.id}
