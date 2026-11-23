"""U-HUB-log (appends): `POST /v1/orgs/{org}/log`: expected_seq and parallel appends, foreign authors and forged
signatures, the rotation obligation R, platform access, accept at the hub, W3 for batch wraps and batch events
(03-api.md section 4.3, 04-crypto.md sections 4.7, 5.2, 5.10, 6.1)."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest

from agent_hub.crypto.keys import Identity
from agent_hub.errors import HubError
from agent_hub.log.entries import sign_log_entry
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.orgs.support import (
    Actor,
    OrgDraft,
    PlatformLog,
    World,
    insert_invite,
    insert_platform_entries,
    new_id,
)


def _grant(draft: OrgDraft, admin: Actor, user: Actor, wg: str, access: str = "read") -> None:
    draft.add("grant", admin, subject=user.id, wg=wg, access=access)


def _new_wg(world: World, draft: OrgDraft, admin: Actor) -> str:
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    response = draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin]))
    assert response.status_code == 200, response.text
    return wg


def test_grant_appends_and_projects(world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    admin = draft.founder
    member = world.user()
    world.join(draft, member)
    wg = _new_wg(world, draft, admin)
    published.clear()
    _grant(draft, admin, member, wg)
    response = draft.post(admin, draft.wraps(admin, "wg", wg, 1, [member]))
    assert response.status_code == 200, response.text
    assert response.json() == {"head": {"seq": 4, "hash": world.rows("SELECT log_head_hash FROM hub.org")[0][0]}}
    assert world.rows("SELECT access, granted_seq FROM hub.wg_access WHERE wg_id = %s AND user_id = %s", wg, member.id)
    frames = [(item.audience.kind, str(item.audience.id), item.frame) for item in published.items()]
    assert ("org", draft.id, "log") in frames
    assert ("user", member.id, "wraps") in frames
    actions = [row[0] for row in world.rows("SELECT action FROM hub.audit WHERE action LIKE 'log.%%' ORDER BY seq")]
    assert actions == ["log.genesis", "log.invite", "log.accept", "log.wg_create", "log.grant"]


def test_expected_seq_mismatch_is_seq_conflict(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    draft.add("wg_create", admin, wg=new_id(), epoch=1)
    response = draft.post(admin, expected_seq=5)
    assert (response.status_code, response.json()) == (409, {"error": "seq_conflict", "detail": {"expected_seq": 1}})


def test_parallel_appends_one_wins_the_other_gets_seq_conflict(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    bodies = []
    for _ in range(2):
        wg = new_id()
        entry = draft.add("wg_create", admin, wg=wg, epoch=1)
        draft.entries.pop()
        bodies.append(
            {"expected_seq": 1, "entries": [entry], "wraps": draft.wraps(admin, "wg", wg, 1, [admin]), "events": []}
        )
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def send(body: dict[str, object]) -> None:
        client = world.hub.browser(uuid.UUID(admin.id))
        barrier.wait()
        statuses.append(client.post(f"/v1/orgs/{draft.id}/log", json=body).status_code)

    threads = [threading.Thread(target=send, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200, 409]
    assert world.rows("SELECT log_head_seq FROM hub.org") == [(1,)]


def test_entries_of_another_author_are_refused(world: World) -> None:
    draft = world.public_org()
    member = world.user()
    world.join(draft, member)
    draft.add("wg_create", draft.founder, wg=new_id(), epoch=1)
    assert draft.post(member, client=member.browser).json() == {"error": "forbidden"}


def test_forged_signature_and_missing_rights_are_refused(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member = world.user()
    world.join(draft, member)
    wg = new_id()
    draft.add("wg_create", admin, signer=Identity.generate(), wg=wg, epoch=1)
    response = draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin]))
    assert (response.status_code, response.json()) == (400, {"error": "log_signature"})
    draft.drop_pending()
    draft.add("wg_create", member, wg=wg, epoch=1)
    assert draft.post(member, draft.wraps(member, "wg", wg, 1, [member])).json() == {"error": "log_rights"}


def test_unknown_org_and_malformed_ids(world: World) -> None:
    user = world.user()
    body = {"expected_seq": 1, "entries": [{"body": {}, "sig": "x"}], "wraps": [], "events": []}
    unknown = user.browser.post(f"/v1/orgs/{uuid.uuid4()}/log", json=body, extensions={"contract": False})
    assert (unknown.status_code, unknown.json()) == (404, {"error": "not_found"})
    malformed = user.browser.post("/v1/orgs/not-an-id/log", json=body, extensions={"contract": False})
    assert malformed.json() == {"error": "not_found"}


def test_invite_entries_only_through_the_invitations_endpoint(world: World) -> None:
    draft = world.public_org()
    draft.invite(draft.founder)
    response = draft.post(draft.founder)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/entries/0"]}}


def test_remove_needs_the_rotate_set_and_deletes_the_removed_wraps(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member = world.user()
    world.join(draft, member)
    wg = _new_wg(world, draft, admin)
    _grant(draft, admin, member, wg)
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [member])).status_code == 200
    stored = draft.wraps(admin, "org", draft.id, 1, [member]) + draft.wraps(admin, "wg", draft.default_wg, 1, [member])
    assert world.hub.client(admin.browser.key).post("/v1/wraps", json={"wraps": stored}).status_code == 200
    draft.add("remove", admin, subject=member.id)
    assert draft.post(admin).json() == {"error": "rotation_required"}
    draft.add("rotate", admin, scope="org", scope_id=draft.id, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=draft.default_wg, epoch=2)
    incomplete = draft.wraps(admin, "org", draft.id, 2, [admin]) + draft.wraps(
        admin, "wg", draft.default_wg, 2, [admin]
    )
    assert draft.post(admin, incomplete).json() == {"error": "rotation_required"}
    draft.add("rotate", admin, scope="wg", scope_id=wg, epoch=2)
    wraps = incomplete + draft.wraps(admin, "wg", wg, 2, [admin])
    response = draft.post(admin, wraps)
    assert response.status_code == 200, response.text
    assert world.rows("SELECT count(*) FROM hub.key_wrap WHERE recipient_id = %s", member.id) == [(0,)]
    assert world.rows("SELECT removed_seq FROM hub.org_member WHERE principal_id = %s", member.id) == [(5,)]
    revoked = world.rows("SELECT wg_id::text, revoked_seq FROM hub.wg_access WHERE user_id = %s ORDER BY 1", member.id)
    assert {row[1] for row in revoked} == {5}


def test_ungrant_needs_exactly_the_workgroup_rotation(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member = world.user()
    world.join(draft, member)
    wg = _new_wg(world, draft, admin)
    _grant(draft, admin, member, wg, "write")
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 1, [member])).status_code == 200
    draft.add("ungrant", admin, subject=member.id, wg=wg)
    draft.add("rotate", admin, scope="org", scope_id=draft.id, epoch=2)
    assert draft.post(admin, draft.wraps(admin, "org", draft.id, 2, [admin, member])).json() == {
        "error": "rotation_required"
    }
    draft.drop_pending()
    draft.add("ungrant", admin, subject=member.id, wg=wg)
    draft.add("rotate", admin, scope="wg", scope_id=wg, epoch=2)
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 2, [admin])).status_code == 200


def test_device_unassign_rotates_the_org_scope_only_with_the_last_workgroup(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    device = world.device()
    wg = _new_wg(world, draft, admin)
    found = draft.invite(admin, kind="device", wgs={wg: "device", draft.default_wg: "device"})
    draft.append(admin)
    draft.accept(device, found)
    draft.append(device)
    draft.add("device_unassign", admin, device=device.id, wg=wg)
    draft.add("rotate", admin, scope="wg", scope_id=wg, epoch=2)
    assert draft.post(admin, draft.wraps(admin, "wg", wg, 2, [admin])).status_code == 200
    draft.add("device_unassign", admin, device=device.id, wg=draft.default_wg)
    draft.add("rotate", admin, scope="wg", scope_id=draft.default_wg, epoch=2)
    assert draft.post(admin, draft.wraps(admin, "wg", draft.default_wg, 2, [admin])).json() == {
        "error": "rotation_required"
    }
    draft.add("rotate", admin, scope="org", scope_id=draft.id, epoch=2)
    wraps = draft.wraps(admin, "wg", draft.default_wg, 2, [admin]) + draft.wraps(admin, "org", draft.id, 2, [admin])
    assert draft.post(admin, wraps).status_code == 200
    assert world.rows("SELECT count(*) FROM hub.wg_device WHERE unassigned_seq IS NULL") == [(0,)]


def test_platform_device_needs_platform_access(world: World) -> None:
    device = world.device("platform")
    log = PlatformLog()
    certified = log.certify(device)
    insert_platform_entries(world.hub.pool, log.entries, world.hub.clock())
    public = world.public_org()
    public.add("device_assign", public.founder, device=device.id, wg=public.default_wg, platform_seq=certified)
    wraps = public.wraps(public.founder, "wg", public.default_wg, 1, [device])
    response = public.post(public.founder, wraps)
    assert (response.status_code, response.json()) == (403, {"error": "platform_access_denied"})
    private = world.private_org()
    admin = private.founder
    private.add("device_assign", admin, device=device.id, wg=private.default_wg, platform_seq=certified)
    wraps = private.wraps(admin, "wg", private.default_wg, 1, [device]) + private.wraps(
        admin, "org", private.id, 1, [device]
    )
    assert private.post(admin, wraps).status_code == 200
    assert world.rows("SELECT kind, role, joined_seq FROM hub.org_member WHERE principal_id = %s", device.id) == [
        ("device", "device", 1)
    ]
    view = admin.browser.get(f"/v1/orgs/{private.id}").json()
    assert view["workgroups"][0]["devices"] == [device.id]
    assert view["rotation_pending"] == []
    log.add("device_decertify", device=device.id)
    insert_platform_entries(world.hub.pool, log.entries[-1:], world.hub.clock(), first=len(log.entries) - 1)
    private.add("grant", admin, subject=admin.id, wg=private.default_wg, access="read")
    assert private.post(admin).json() == {"error": "log_rights"}
    private.drop_pending()
    other = world.user()
    world.join(private, other)
    view = admin.browser.get(f"/v1/orgs/{private.id}").json()
    assert view["rotation_pending"] == sorted(
        [{"scope": "org", "scope_id": private.id}, {"scope": "wg", "scope_id": private.default_wg}],
        key=lambda item: (item["scope"], item["scope_id"]),
    )
    private.add("device_unassign", admin, device=device.id, wg=private.default_wg)
    private.add("rotate", admin, scope="wg", scope_id=private.default_wg, epoch=2)
    private.add("rotate", admin, scope="org", scope_id=private.id, epoch=2)
    wraps = private.wraps(admin, "wg", private.default_wg, 2, [admin, other]) + private.wraps(
        admin, "org", private.id, 2, [admin, other]
    )
    assert private.post(admin, wraps).status_code == 200
    assert admin.browser.get(f"/v1/orgs/{private.id}").json()["rotation_pending"] == []


def test_assigning_a_decertified_device_is_refused(world: World) -> None:
    device = world.device("platform")
    log = PlatformLog()
    certified = log.certify(device)
    log.add("device_decertify", device=device.id)
    insert_platform_entries(world.hub.pool, log.entries, world.hub.clock())
    private = world.private_org()
    private.add("device_assign", private.founder, device=device.id, wg=private.default_wg, platform_seq=certified)
    assert private.post(private.founder).json() == {"error": "log_rights"}


def test_user_accept_checks_identity_and_registration(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    invitee, other = world.user(), world.user()
    found = draft.invite(admin)
    draft.append(admin)
    insert_invite(
        world.hub.pool, found.invite_id, world.hub.clock(), scope="org", org_id=draft.id, registered_user=other.id
    )
    draft.accept(invitee, found)
    with pytest.raises(HubError) as refused:
        draft.append(invitee)
    assert refused.value.code == "invite_registered"
    draft.drop_pending()
    draft.accept(other, found, Identity.generate())
    with pytest.raises(HubError) as refused:
        draft.append(other)
    assert refused.value.code == "log_accept"
    draft.drop_pending()
    draft.accept(other, found)
    response = draft.post(other)
    assert response.status_code == 200, response.text
    assert world.rows("SELECT used_by::text, package_ct FROM hub.invite WHERE id = %s", found.invite_id) == [
        (other.id, None)
    ]
    assert world.rows(
        "SELECT actor_kind, actor_id::text, org_id::text, device_id, target_kind, target_id, detail FROM hub.audit"
        " WHERE action = 'invite.use'"
    ) == [("user", other.id, draft.id, None, "invite", found.invite_id, {"invite_id": found.invite_id, "kind": "user"})]


def test_accept_of_an_expired_invitation(world: World) -> None:
    draft = world.public_org()
    invitee = world.user()
    found = draft.invite(draft.founder, expires="2026-11-21T12:00:01.000Z")
    draft.append(draft.founder)
    draft.accept(invitee, found)
    with pytest.raises(HubError) as refused:
        draft.append(invitee, now=world.hub.clock() + timedelta(seconds=5))
    assert (refused.value.code, refused.value.status) == ("invite_expired", 410)


def test_leaving_member_leaves_a_pending_rotation(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member = world.user()
    world.join(draft, member)
    draft.add("remove", member, subject=member.id)
    assert draft.post(member).status_code == 200
    pending = admin.browser.get(f"/v1/orgs/{draft.id}").json()["rotation_pending"]
    assert {item["scope"] for item in pending} == {"org", "wg"}
    draft.add("rotate", admin, scope="org", scope_id=draft.id, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=draft.default_wg, epoch=2)
    wraps = draft.wraps(admin, "org", draft.id, 2, [admin]) + draft.wraps(admin, "wg", draft.default_wg, 2, [admin])
    assert draft.post(admin, wraps).status_code == 200
    assert admin.browser.get(f"/v1/orgs/{draft.id}").json()["rotation_pending"] == []
    assert member.browser.get(f"/v1/orgs/{draft.id}").json() == {"error": "not_found"}


def test_batch_wraps_follow_w3(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    member, stranger = world.user(), world.user()
    world.join(draft, member)
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    wraps = draft.wraps(admin, "wg", wg, 1, [admin, stranger])
    assert draft.post(admin, wraps).json() == {"error": "wrap_unauthorized"}


def test_batch_events_are_stored_with_the_entries(world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    admin = draft.founder
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    profile = draft.event(admin, "wg.profile", wg=wg)
    published.clear()
    response = draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin]), [profile, profile])
    assert response.status_code == 200, response.text
    rows = world.rows("SELECT kind, scope_seq, lh, wg_id::text FROM hub.event WHERE id = %s", profile["header"]["id"])
    assert rows == [("wg.profile", 4, 1, wg)]
    assert [item.frame for item in published.items()].count("event") == 1


def test_batch_events_are_checked_against_the_log_after_the_batch(world: World) -> None:
    draft = world.public_org()
    admin = draft.founder
    stored = draft.event(admin, "org.profile", lh=0)
    draft.add("grant", admin, subject=admin.id, wg=new_id(), access="read")
    draft.drop_pending()
    wg = new_id()
    draft.add("wg_create", admin, wg=wg, epoch=1)
    wraps = draft.wraps(admin, "wg", wg, 1, [admin])
    assert draft.post(admin, wraps, [draft.event(admin, "org.profile", lh=99)]).json() == {"error": "header_invalid"}
    assert draft.post(admin, wraps, [stored]).status_code == 200
    conflicting = dict(stored, sig=draft.event(admin, "org.profile", lh=0)["sig"])
    draft.add("wg_archive", admin, wg=wg)
    response = draft.post(admin, events=[conflicting])
    assert response.json() in ({"error": "signature_invalid"}, {"error": "event_conflict"})


def test_browser_cannot_accept_a_device_invitation(world: World) -> None:
    draft = world.public_org()
    intruder = world.user()
    found = draft.invite(draft.founder, kind="device", wgs={draft.default_wg: "device"})
    draft.append(draft.founder)
    body = draft.body(
        "accept", intruder, invite_id=found.invite_id, subject=intruder.id, kind="device", pub=intruder.pub
    )
    draft.entries.append(sign_log_entry(body, intruder.identity.sig, found.signing))
    response = draft.post(intruder)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/entries/0"]}}


def test_seq_conflict_discloses_the_head_to_members_only(world: World) -> None:
    draft = world.public_org()
    stranger = world.user()
    draft.add("remove", stranger, subject=stranger.id)
    response = draft.post(stranger, expected_seq=7)
    assert (response.status_code, response.json()) == (409, {"error": "seq_conflict"})
