"""U-HUB-log (genesis): `POST /v1/orgs`: private orgs with a platform invitation, non-private orgs, idempotency,
the founder key, the self-wraps and the profile events (03-api.md section 4.3, 04-crypto.md section 5.3, ADR-026)."""

from __future__ import annotations

import copy

from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import Identity
from agent_hub.log.entries import sign_log_entry
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.orgs.support import (
    PlatformLog,
    World,
    insert_invite,
    insert_platform_entries,
    new_id,
    new_invite_id,
)


def test_private_genesis_consumes_the_platform_invitation(world: World, published: MemoryPublisher) -> None:
    draft = world.org(private=True)
    invite_id = world.platform_invite(draft.founder)
    response = draft.create(invite_id)
    assert response.status_code == 200, response.text
    assert response.json() == {"org_id": draft.id, "default_wg": draft.default_wg}
    founder = draft.founder.id
    assert world.rows("SELECT private, platform_access, created_by::text, log_head_seq FROM hub.org") == [
        (True, True, founder, 0)
    ]
    assert world.rows("SELECT used_by::text, package_ct FROM hub.invite WHERE id = %s", invite_id) == [(founder, None)]
    assert world.rows("SELECT principal_id::text, kind, role, joined_seq, removed_seq FROM hub.org_member") == [
        (founder, "user", "admin", 0, None)
    ]
    assert world.rows("SELECT id::text, is_default, created_seq FROM hub.wg") == [(draft.default_wg, True, 0)]
    assert world.rows("SELECT wg_id::text, user_id::text, access, granted_seq FROM hub.wg_access") == [
        (draft.default_wg, founder, "write", 0)
    ]
    assert sorted(world.rows("SELECT scope, scope_id::text, epoch, created_seq FROM hub.scope_epoch")) == sorted(
        [("org", draft.id, 1, 0), ("wg", draft.default_wg, 1, 0)]
    )
    assert len(world.rows("SELECT 1 FROM hub.key_wrap WHERE recipient_id = %s", founder)) == 2
    assert sorted(row[0] for row in world.rows("SELECT kind FROM hub.event")) == [
        "member.profile",
        "org.profile",
        "wg.profile",
    ]
    actions = [row[0] for row in world.rows("SELECT action FROM hub.audit ORDER BY seq")]
    assert actions == ["invite.use", "log.genesis", "event.append", "event.append", "event.append"]
    assert world.rows(
        "SELECT actor_kind, actor_id::text, org_id::text, target_kind, target_id, detail FROM hub.audit"
        " WHERE action = 'invite.use'"
    ) == [("user", founder, draft.id, "invite", invite_id, {"invite_id": invite_id, "kind": "user"})]
    frames = [(item.audience.kind, item.frame) for item in published.items()]
    assert ("org", "log") in frames
    assert ("user", "wraps") in frames
    assert frames.count(("org", "event")) == 3


def test_second_private_org_needs_a_fresh_invitation(world: World) -> None:
    first = world.private_org()
    invite_id = world.rows("SELECT id FROM hub.invite")[0][0]
    again = world.org(first.founder, private=True)
    response = again.create(invite_id)
    assert (response.status_code, response.json()) == (403, {"error": "platform_invite_required"})
    response = again.create()
    assert response.json() == {"error": "platform_invite_required"}


def test_private_genesis_refuses_foreign_revoked_and_used_invitations(world: World) -> None:
    other = world.user()
    for owner, revoked, used in ((other, False, False), (None, True, False), (None, False, True)):
        draft = world.org(private=True)
        invite_id = new_invite_id()
        user = (owner or draft.founder).id
        insert_invite(world.hub.pool, invite_id, world.hub.clock(), registered_user=user, revoked=revoked, used=used)
        response = draft.create(invite_id)
        assert response.json() == {"error": "platform_invite_required"}
    unbound = world.org(private=True)
    unbound_id = new_invite_id()
    insert_invite(world.hub.pool, unbound_id, world.hub.clock())
    assert unbound.create(unbound_id).json() == {"error": "platform_invite_required"}
    org_invite = world.org(private=True)
    org_invite_id = new_invite_id()
    insert_invite(
        world.hub.pool,
        org_invite_id,
        world.hub.clock(),
        scope="org",
        org_id=new_id(),
        registered_user=org_invite.founder.id,
    )
    assert org_invite.create(org_invite_id).json() == {"error": "platform_invite_required"}


def test_non_private_genesis_has_no_platform_access_until_granted(world: World) -> None:
    draft = world.public_org()
    assert world.rows("SELECT private, platform_access FROM hub.org WHERE id = %s", draft.id) == [(False, False)]
    log = PlatformLog()
    granted = world.org()
    log.add("root", pub=_root_pub(log))
    log.add("org_platform_access", org=granted.id, granted=True)
    insert_platform_entries(world.hub.pool, log.entries, world.hub.clock())
    assert granted.create().status_code == 200
    assert world.rows("SELECT platform_access FROM hub.org WHERE id = %s", granted.id) == [(True,)]


def _root_pub(log: PlatformLog) -> str:
    return b64u_encode(log.root.public_key().public_bytes_raw())


def test_platform_invite_id_on_a_non_private_genesis_is_refused(world: World) -> None:
    draft = world.org()
    response = draft.create(world.platform_invite(draft.founder))
    assert (response.status_code, response.json()) == (400, {"error": "request_invalid"})


def test_genesis_is_idempotent_and_ids_are_not_reused(world: World) -> None:
    draft = world.public_org()
    again = draft.founder.browser.post("/v1/orgs", json=draft.create_body())
    assert again.json() == {"org_id": draft.id, "default_wg": draft.default_wg, "replayed": True}
    other = world.org()
    other.id = draft.id
    assert other.create().json() == {"error": "id_conflict"}
    changed = world.org(draft.founder)
    changed.id = draft.id
    assert changed.create().json() == {"error": "id_conflict"}
    reused_wg = world.org()
    reused_wg.default_wg = draft.default_wg
    assert reused_wg.create().json() == {"error": "id_conflict"}
    wg_as_org = world.org()
    wg_as_org.default_wg = draft.id
    assert wg_as_org.create().json() == {"error": "id_conflict"}


def test_genesis_must_carry_the_callers_registered_identity(world: World) -> None:
    unregistered = world.org(world.user(registered=False))
    assert unregistered.create().json() == {"error": "log_genesis"}
    draft = world.org()
    stranger = Identity.generate()
    draft.add(
        "genesis",
        draft.founder,
        subject=draft.founder.id,
        pub=stranger.public(),
        default_wg=draft.default_wg,
        private=False,
    )
    response = draft.founder.browser.post("/v1/orgs", json=draft.create_body())
    assert (response.status_code, response.json()) == (400, {"error": "log_genesis"})


def test_genesis_by_another_principal_and_malformed_entries_are_refused(world: World) -> None:
    draft = world.org()
    other = world.user()
    body = draft.create_body()
    assert other.browser.post("/v1/orgs", json=body).json() == {"error": "forbidden"}
    not_genesis = world.org()
    not_genesis.add("wg_create", not_genesis.founder, wg=new_id(), epoch=1)
    assert not_genesis.create().json() == {"error": "log_genesis"}
    broken = world.org()
    body = broken.create_body()
    del body["genesis"]["body"]["private"]
    response = broken.founder.browser.post("/v1/orgs", json=body, extensions={"contract": False})
    assert response.json() == {"error": "header_invalid"}
    forged = world.org()
    forged.genesis()
    forged.entries[0] = sign_log_entry(forged.entries[0]["body"], Identity.generate().sig)
    assert forged.create().json() == {"error": "log_genesis"}


def test_genesis_needs_the_founders_self_wraps(world: World) -> None:
    draft = world.org()
    body = draft.create_body()
    body["wraps"] = body["wraps"][:1]
    assert draft.founder.browser.post("/v1/orgs", json=body).json() == {"error": "rotation_required"}
    other = world.user()
    body = draft.create_body()
    body["wraps"].append(draft.wrap(draft.founder, "org", draft.id, 1, other))
    assert draft.founder.browser.post("/v1/orgs", json=body).json() == {"error": "wrap_unauthorized"}
    assert world.rows("SELECT count(*) FROM hub.org") == [(0,)]


def test_genesis_events_are_exactly_the_profiles(world: World) -> None:
    draft = world.org()
    body = draft.create_body()
    body["events"][0] = draft.event(
        draft.founder, "settings.workgroup", scope="wg", scope_id=draft.default_wg, lh=0, wg=draft.default_wg
    )
    assert draft.founder.browser.post("/v1/orgs", json=body).json() == {"error": "kind_not_allowed"}
    body = draft.create_body()
    body["events"][2] = draft.event(draft.founder, "org.profile", lh=0)
    assert draft.founder.browser.post("/v1/orgs", json=body).json() == {"error": "request_invalid"}
    body = draft.create_body()
    body["events"][0] = {"header": "x", "ct": "x", "sig": "x"}
    response = draft.founder.browser.post("/v1/orgs", json=body, extensions={"contract": False})
    assert response.json() == {"error": "header_invalid"}
    body = draft.create_body()
    tampered = copy.deepcopy(body["events"][0])
    tampered["header"]["ts"] = "2026-11-21T12:00:01.000Z"
    body["events"][0] = tampered
    assert draft.founder.browser.post("/v1/orgs", json=body).json() == {"error": "signature_invalid"}
    assert world.rows("SELECT count(*) FROM hub.org") == [(0,)]


def test_genesis_events_must_be_authored_by_the_founder(world: World) -> None:
    draft = world.org()
    body = draft.create_body()
    other = world.user()
    body["events"][2] = draft.event(other, "member.profile", lh=0, ref=other.id)
    assert draft.founder.browser.post("/v1/orgs", json=body).json() == {"error": "forbidden"}
