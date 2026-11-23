"""U-HUB-log (direct admission, ADR-060): `admit` through `POST /v1/orgs/{org}/log` - the hub checks of
`agent_hub.orgs.admit` (registered identity, `409 peer_outdated`), the wrap rule of the batch, the notification
`org.admitted`, the projections and the missing consent (no credential key for the org's devices)."""

from __future__ import annotations

from collections.abc import Mapping

from agent_hub.crypto import wrap as wrap_format
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import Identity
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import raw_public
from tests.platform.hub.orgs.support import Actor, OrgDraft, PlatformLog, World, insert_platform_entries, new_id

Json = dict[str, object]


class Anchors:
    """The platform log of the test hub with the identity certifications of users (`user_certify`)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.log = PlatformLog()
        self.stored = 0
        self.log.add("root", pub=b64u_encode(raw_public(self.log.root)))

    def certify(self, actor: Actor, pub: Mapping[str, str] | None = None) -> int:
        self.log.add("user_certify", user=actor.id, pub=dict(pub or actor.pub))
        hub = self.world.hub
        insert_platform_entries(hub.pool, self.log.entries[self.stored :], hub.clock(), first=self.stored)
        self.stored = len(self.log.entries)
        return self.stored - 1


def _admit(
    draft: OrgDraft,
    admin: Actor,
    admin_seq: int,
    user: Actor,
    user_seq: int,
    *,
    role: str = "member",
    wgs: Mapping[str, str] | None = None,
    pub: Mapping[str, str] | None = None,
) -> Json:
    return draft.add(
        "admit",
        admin,
        subject=user.id,
        pub=dict(pub or user.pub),
        platform_seq=user_seq,
        id_sig="",
        by_platform_seq=admin_seq,
        by_id_sig="",
        role=role,
        wgs=dict(wgs or {}),
    )


def _wraps(draft: OrgDraft, admin: Actor, user: Actor, epoch: int = 1) -> list[Json]:
    return draft.wraps(admin, "org", draft.id, epoch, [user]) + draft.wraps(
        admin, "wg", draft.default_wg, epoch, [user]
    )


def _setup(world: World) -> tuple[OrgDraft, Actor, Actor, Anchors, int, int]:
    draft = world.public_org()
    admin, user = draft.founder, world.user()
    anchors = Anchors(world)
    return draft, admin, user, anchors, anchors.certify(admin), anchors.certify(user)


def _features(world: World, device: Actor, *features: str) -> None:
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.device SET features = %s WHERE id = %s", (list(features), device.id))


def test_admit_with_wraps_makes_a_member_and_notifies_it(world: World, published: MemoryPublisher) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin])).status_code == 200
    published.clear()
    _admit(draft, admin, admin_seq, user, user_seq, wgs={wg: "read"})
    response = draft.post(admin, _wraps(draft, admin, user) + draft.wraps(admin, "wg", wg, 1, [user]))
    assert response.status_code == 200, response.text
    assert world.rows(
        "SELECT recipient_id::text, kind, org_id::text, wg_id, task_id, ref FROM hub.notification"
        " WHERE recipient_id = %s",
        user.id,
    ) == [(user.id, "org.admitted", draft.id, None, None, admin.id)]
    frames = [(item.audience.kind, str(item.audience.id), item.frame) for item in published.items()]
    assert ("user", user.id, "notification") in frames
    assert ("user", user.id, "wraps") in frames
    actions = [row[0] for row in world.rows("SELECT action FROM hub.audit WHERE action LIKE 'log.%%' ORDER BY seq")]
    assert actions[-1] == "log.admit"
    assert world.rows(
        "SELECT kind, role, joined_seq, removed_seq FROM hub.org_member WHERE principal_id = %s", user.id
    ) == [("user", "member", 2, None)]
    access = world.rows("SELECT wg_id::text, access FROM hub.wg_access WHERE user_id = %s", user.id)
    assert sorted(access) == sorted([(draft.default_wg, "write"), (wg, "read")])
    orgs = user.browser.get("/v1/me").json()["orgs"]
    assert [(item["org_id"], item["role"]) for item in orgs] == [(draft.id, "member")]
    assert user.browser.get(f"/v1/orgs/{draft.id}").status_code == 200


def test_admit_of_keys_that_are_not_the_registered_identity(world: World) -> None:
    draft = world.public_org()
    admin, user = draft.founder, world.user()
    anchors = Anchors(world)
    admin_seq = anchors.certify(admin)
    other = Identity.generate().public()
    seq = anchors.certify(user, other)  # keys the hub swapped in: anchored by the root, but not the registered ones
    _admit(draft, admin, admin_seq, user, seq, pub=other)
    assert draft.post(admin, _wraps(draft, admin, user)).json() == {"error": "log_admit"}


def test_admit_of_a_disabled_account(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", (world.hub.clock(), user.id))
    _admit(draft, admin, admin_seq, user, user_seq)
    assert draft.post(admin, _wraps(draft, admin, user)).json() == {"error": "log_admit"}


def test_admit_without_the_wraps_of_the_org_and_the_default_workgroup(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    _admit(draft, admin, admin_seq, user, user_seq)
    assert draft.post(admin).json() == {"error": "log_admit"}
    assert draft.post(admin, draft.wraps(admin, "org", draft.id, 1, [user])).json() == {"error": "log_admit"}
    assert draft.post(admin, draft.wraps(admin, "wg", draft.default_wg, 1, [user])).json() == {"error": "log_admit"}
    assert world.rows("SELECT count(*) FROM hub.notification WHERE recipient_id = %s", user.id) == [(0,)]


def test_admit_by_a_member_is_refused(world: World) -> None:
    draft, _, user, anchors, _, user_seq = _setup(world)
    member = world.user()
    world.join(draft, member)
    member_seq = anchors.certify(member)
    _admit(draft, member, member_seq, user, user_seq)
    assert draft.post(member, _wraps(draft, member, user)).json() == {"error": "log_rights"}


def test_every_active_device_must_announce_the_feature(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    device, idle = world.device(), world.device()
    world.join(draft, device, wgs={draft.default_wg: "device"})
    world.join(draft, idle)
    _features(world, device, "log.admit")
    _admit(draft, admin, admin_seq, user, user_seq)
    response = draft.post(admin, _wraps(draft, admin, user))
    assert (response.status_code, response.json()) == (
        409,
        {"error": "peer_outdated", "detail": {"devices": [idle.id]}},
    )
    _features(world, idle, "log.admit", "other")
    assert draft.post(admin, _wraps(draft, admin, user)).status_code == 200


def test_assigning_a_device_without_the_feature_into_an_org_with_an_admit(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    device = world.device()
    world.join(draft, device)
    _features(world, device, "log.admit")
    _admit(draft, admin, admin_seq, user, user_seq)
    assert draft.post(admin, _wraps(draft, admin, user)).status_code == 200
    _features(world, device)  # downgraded
    draft.add("device_assign", admin, device=device.id, wg=draft.default_wg)
    wraps = draft.wraps(admin, "wg", draft.default_wg, 1, [device]) + draft.wraps(admin, "org", draft.id, 1, [device])
    response = draft.post(admin, wraps)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "peer_outdated", "detail": {"devices": [device.id]}},
    )
    _features(world, device, "log.admit")
    assert draft.post(admin, wraps).status_code == 200


def test_device_assign_in_an_org_without_admit_needs_no_feature(world: World) -> None:
    draft = world.public_org()
    admin, device = draft.founder, world.device()
    world.join(draft, device)
    draft.add("device_assign", admin, device=device.id, wg=draft.default_wg)
    wraps = draft.wraps(admin, "wg", draft.default_wg, 1, [device]) + draft.wraps(admin, "org", draft.id, 1, [device])
    assert draft.post(admin, wraps).status_code == 200


def test_the_admitted_user_may_leave(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    _admit(draft, admin, admin_seq, user, user_seq, role="admin")
    assert draft.post(admin, _wraps(draft, admin, user)).status_code == 200
    draft.add("remove", user, subject=user.id)
    assert draft.post(user).status_code == 200
    assert world.rows("SELECT removed_seq FROM hub.org_member WHERE principal_id = %s", user.id) == [(2,)]
    assert user.browser.get("/v1/me").json()["orgs"] == []


def test_admit_and_remove_in_one_batch_owes_no_wraps_for_the_removed(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    _admit(draft, admin, admin_seq, user, user_seq)
    draft.add("remove", admin, subject=user.id)
    draft.add("rotate", admin, scope="org", scope_id=draft.id, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=draft.default_wg, epoch=2)
    response = draft.post(admin, _wraps(draft, admin, admin, epoch=2))
    assert response.status_code == 200, response.text
    assert world.rows("SELECT joined_seq, removed_seq FROM hub.org_member WHERE principal_id = %s", user.id) == [(1, 2)]


def _cred_wrap(draft: OrgDraft, owner: Actor, recipient: Actor) -> Json:
    sealed = wrap_format.wrap_key(
        recipient.identity.kem_public(),
        scope="cred",
        scope_id=owner.id,
        epoch=1,
        recipient=recipient.id,
        key=draft.key("cred", owner.id, 1),
    )
    signature = wrap_format.sign_wrap(
        owner.identity.sig, scope="cred", scope_id=owner.id, epoch=1, recipient=recipient.id, wrap=sealed
    )
    return {
        "scope": "cred",
        "scope_id": owner.id,
        "epoch": 1,
        "recipient_id": recipient.id,
        "wrap": sealed,
        "sig": signature,
    }


def test_the_devices_of_an_org_that_admitted_a_user_get_no_credential_key(world: World) -> None:
    draft, admin, user, _, admin_seq, user_seq = _setup(world)
    device = world.device()
    world.join(draft, device, wgs={draft.default_wg: "device"})
    _features(world, device, "log.admit")
    _admit(draft, admin, admin_seq, user, user_seq)
    assert draft.post(admin, _wraps(draft, admin, user)).status_code == 200
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.scope_epoch (scope, scope_id, epoch, created_at) VALUES ('cred', %s, 1, %s)",
            (user.id, world.hub.clock()),
        )
    refused = user.browser.post("/v1/wraps", json={"wraps": [_cred_wrap(draft, user, device)]})
    assert refused.json() == {"error": "wrap_unauthorized"}
    missing = user.browser.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    assert all(item["scope"] != "cred" or item["recipient_id"] == user.id for item in missing)
    assert user.browser.get("/v1/me/devices").json()["devices"] == []
    assert world.rows("SELECT consented FROM hub.org_member WHERE principal_id = %s", user.id) == [(False,)]
    assert world.rows("SELECT consented FROM hub.org_member WHERE principal_id = %s", admin.id) == [(True,)]
