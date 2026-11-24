"""U-HUB-invites: `POST|GET /v1/orgs/{org}/invites`, `POST /v1/orgs/{org}/invites/{id}/revoke` (03-api.md section 4.2,
04-crypto.md sections 4.6, 4.7 and 5.7, ADR-005; R-1.4, R-1.11, R-1.12).

An invitation is created atomically with its `invite` log entry, carries the expiry of the signed entry (defaults
72 h / 24 h chosen by the browser, at most 30 days), is listed with its state and revoked with `invite_revoke`; the
package is deleted on revocation. Every response is validated against `contracts/api/` by the test client.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.signing import b64url_decode
from agent_hub.config import LIMITS
from agent_hub.crypto import invite as invite_format
from agent_hub.crypto.encoding import format_time
from agent_hub.invites import orgs as invite_orgs
from agent_hub.orgs.append import append_batch
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.invites.support import (
    HOUR,
    create_invitation,
    expires_in,
    package_of,
    random_invite_id,
    revoke_invitation,
)
from tests.platform.hub.orgs.support import World

NO_CONTRACT = {"contract": False}


def invite_row(world: World, invite_id: str) -> tuple[Any, ...]:
    (row,) = world.rows(
        "SELECT scope, kind, org_id, log_seq, created_by, created_at, expires_at, used_at, revoked_at, package_ct"
        " FROM hub.invite WHERE id = %s",
        invite_id,
    )
    return row


def audit(world: World, action: str) -> list[tuple[Any, ...]]:
    return world.rows(
        "SELECT actor_kind, actor_id, org_id, target_kind, target_id, detail FROM hub.audit WHERE action = %s"
        " ORDER BY seq",
        action,
    )


# --- create -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "seconds"), [("user", LIMITS["invite_user_seconds"]), ("device", 24 * HOUR)])
def test_create_appends_the_entry_and_stores_the_invitation(
    world: World, published: MemoryPublisher, kind: str, seconds: int
) -> None:
    # R-1.11: the browser's defaults (72 h user, 24 h device) are taken over as signed in the entry.
    draft = world.public_org()
    published.clear()
    invitation = create_invitation(draft, kind=kind, seconds=seconds)
    assert invitation.response.status_code == 200, invitation.response.text
    assert invitation.response.json() == {"invite_id": invitation.invite_id, "seq": 1}
    now = world.hub.clock()
    scope, stored_kind, org, log_seq, created_by, created_at, expires_at, used, revoked, package = invite_row(
        world, invitation.invite_id
    )
    assert (scope, stored_kind, str(org), log_seq, str(created_by)) == ("org", kind, draft.id, 1, draft.founder.id)
    assert (created_at, expires_at - now, used, revoked) == (now, timedelta(seconds=seconds), None, None)
    assert bytes(package) == b64url_decode(invitation.package_ct)
    (stored,) = world.rows("SELECT entry_text, type FROM hub.membership_entry WHERE org_id = %s AND seq = 1", draft.id)
    assert (json.loads(stored[0]), stored[1]) == (invitation.entry, "invite")
    assert audit(world, "invite.create") == [
        (
            "user",
            UUID(draft.founder.id),
            UUID(draft.id),
            "invite",
            invitation.invite_id,
            {"invite_id": invitation.invite_id, "kind": kind},
        )
    ]
    assert [row[4] for row in audit(world, "log.invite")] == [invitation.invite_id]
    assert [(item.frame, item.data["head"]["seq"]) for item in published.items() if item.frame == "log"] == [("log", 1)]


def test_expiry_up_to_thirty_days_is_accepted(world: World) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft, seconds=LIMITS["invite_max_seconds"])
    assert invitation.response.status_code == 200, invitation.response.text


@pytest.mark.parametrize("seconds", [LIMITS["invite_max_seconds"] + 1, 0, -60])
def test_expiry_beyond_thirty_days_or_in_the_past_is_refused(world: World, seconds: int) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft, seconds=seconds)
    assert invitation.response.status_code == 400
    assert invitation.response.json() == {"error": "request_invalid", "detail": {"pointers": ["/entry/body/expires"]}}
    assert world.rows("SELECT count(*) FROM hub.invite")[0][0] == 0
    assert world.rows("SELECT log_head_seq FROM hub.org WHERE id = %s", draft.id) == [(0,)]


def test_identical_repeat_answers_the_original_and_appends_nothing(world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    published.clear()
    body = {"entry": invitation.entry, "package_ct": invitation.package_ct}
    again = draft.founder.browser.post(f"/v1/orgs/{draft.id}/invites", json=body)
    assert (again.status_code, again.json()) == (200, {"invite_id": invitation.invite_id, "seq": 1})
    assert world.rows("SELECT log_head_seq FROM hub.org WHERE id = %s", draft.id) == [(1,)]
    assert published.items() == []
    assert len(audit(world, "invite.create")) == 1


def test_repeat_with_another_package_or_entry_is_a_conflict(world: World) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    other_package = package_of(draft, invitation.secrets, [draft.default_wg])
    response = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites", json={"entry": invitation.entry, "package_ct": other_package}
    )
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    # another entry (here: the next seq) with the same invitation ID
    entry = draft.add(
        "invite",
        draft.founder,
        invite_id=invitation.invite_id,
        pk_inv=invitation.secrets.pk_inv,
        kind="user",
        role="member",
        wgs={},
        expires=expires_in(world, HOUR),
    )
    response = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites", json={"entry": entry, "package_ct": invitation.package_ct}
    )
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})


def test_invitation_id_of_another_org_or_platform_invitation_is_a_conflict(world: World) -> None:
    first = world.public_org()
    invitation = create_invitation(first)
    second = world.public_org()
    entry = second.add(
        "invite",
        second.founder,
        invite_id=invitation.invite_id,
        pk_inv=invitation.secrets.pk_inv,
        kind="user",
        role="member",
        wgs={},
        expires=expires_in(world, HOUR),
    )
    response = second.founder.browser.post(
        f"/v1/orgs/{second.id}/invites", json={"entry": entry, "package_ct": invitation.package_ct}
    )
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})


def test_concurrent_invitation_with_the_same_id_is_a_conflict(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    # Another invitation with this ID is committed between the check and the insert of the row.
    draft = world.public_org()
    found = draft.invite(draft.founder, wgs={}, expires=expires_in(world, HOUR))
    original = append_batch

    def racing(connection: Any, **kwargs: Any) -> Any:
        store = kwargs["extra"]

        def planted(inner: Any, result: Any) -> None:
            with world.hub.pool.connection() as other, other.transaction():
                other.execute(
                    "INSERT INTO hub.invite (id, scope, kind, created_at, expires_at)"
                    " VALUES (%s, 'platform', 'user', %s, %s)",
                    (found.invite_id, world.hub.clock(), world.hub.clock() + timedelta(hours=1)),
                )
            store(inner, result)

        return original(connection, **{**kwargs, "extra": planted})

    monkeypatch.setattr(invite_orgs, "append_batch", racing)
    package = package_of(draft, found, [draft.default_wg])
    response = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites", json={"entry": draft.entries[-1], "package_ct": package}
    )
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert world.rows("SELECT scope FROM hub.invite") == [("platform",)]
    assert world.rows("SELECT log_head_seq FROM hub.org WHERE id = %s", draft.id) == [(0,)]


def test_only_admins_create(world: World) -> None:
    draft = world.public_org()
    member = world.user()
    world.join(draft, member)
    invitation = create_invitation(draft, by=member)
    assert (invitation.response.status_code, invitation.response.json()) == (403, {"error": "forbidden"})
    outsider = world.user()
    invitation = create_invitation(draft, by=outsider)
    assert (invitation.response.status_code, invitation.response.json()) == (403, {"error": "forbidden"})


def test_unknown_org_is_not_found(world: World) -> None:
    draft = world.public_org()
    other = world.org()
    entry = other.add(
        "invite",
        other.founder,
        invite_id=random_invite_id()[:21] + "A",
        pk_inv=draft.founder.pub["sig"],
        kind="user",
        role="member",
        wgs={},
        expires=expires_in(world, HOUR),
    )
    response = draft.founder.browser.post(f"/v1/orgs/{other.id}/invites", json={"entry": entry, "package_ct": "A" * 40})
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    response = draft.founder.browser.post(
        "/v1/orgs/not-a-uuid/invites", json={"entry": entry, "package_ct": "A" * 40}, extensions=NO_CONTRACT
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})


@pytest.mark.parametrize(
    "entry",
    [
        {"body": "x", "sig": "y"},
        {"sig": "y"},
        {"body": {"type": "invite_revoke", "invite_id": "x", "seq": 1}, "sig": "y"},
        {"body": {"type": "invite", "seq": 1}, "sig": "y"},
        {"body": {"type": "invite", "invite_id": "x", "seq": True}, "sig": "y"},
    ],
)
def test_malformed_entries_are_refused(world: World, entry: dict[str, Any]) -> None:
    draft = world.public_org()
    response = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites", json={"entry": entry, "package_ct": "A" * 40}, extensions=NO_CONTRACT
    )
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/entry"]}},
    )


def test_log_rules_apply(world: World) -> None:
    draft = world.public_org()
    found = draft.invite(draft.founder, wgs={}, expires=expires_in(world, HOUR))
    entry = dict(draft.entries[-1])
    entry["sig"] = draft.entries[0]["sig"]  # a signature over another body
    package = package_of(draft, found, [draft.default_wg])
    response = draft.founder.browser.post(f"/v1/orgs/{draft.id}/invites", json={"entry": entry, "package_ct": package})
    assert (response.status_code, response.json()["error"]) == (400, "log_signature")
    assert world.rows("SELECT count(*) FROM hub.invite") == [(0,)]


def test_package_shape_is_checked(world: World) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft, package_ct="A" * 37, extensions=NO_CONTRACT)
    assert invitation.response.status_code == 400
    assert invitation.response.json()["error"] == "request_invalid"


def test_the_log_route_still_refuses_invite_entries(world: World) -> None:
    draft = world.public_org()
    draft.invite(draft.founder, wgs={}, expires=expires_in(world, HOUR))
    response = draft.post(draft.founder)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


# --- list -------------------------------------------------------------------------------------------------------------


def test_list_shows_every_state_newest_first(world: World, browser_accept: Any) -> None:
    draft = world.public_org()
    expired = create_invitation(draft, seconds=HOUR)
    world.hub.clock.advance(1)
    revoked = create_invitation(draft)
    assert revoke_invitation(draft, revoked).status_code == 200
    world.hub.clock.advance(1)
    used, user = browser_accept(draft)
    world.hub.clock.advance(1)
    device = create_invitation(draft, kind="device", seconds=24 * HOUR)
    world.hub.clock.advance(HOUR)
    response = draft.founder.browser.get(f"/v1/orgs/{draft.id}/invites")
    assert response.status_code == 200, response.text
    listed = response.json()["invites"]
    assert [(item["invite_id"], item["state"], item["kind"]) for item in listed] == [
        (device.invite_id, "open", "device"),
        (used.invite_id, "used", "user"),
        (revoked.invite_id, "revoked", "user"),
        (expired.invite_id, "expired", "user"),
    ]
    by_id = {item["invite_id"]: item for item in listed}
    assert by_id[used.invite_id]["used_by"] == user.id
    assert by_id[used.invite_id]["registered"] is True
    assert by_id[used.invite_id]["registered_user"] == user.id
    assert by_id[used.invite_id]["used_at"] is not None
    assert by_id[revoked.invite_id]["revoked_at"] is not None
    assert by_id[device.invite_id] == {
        "invite_id": device.invite_id,
        "kind": "device",
        "state": "open",
        "log_seq": 6,
        "created_by": draft.founder.id,
        "created_at": format_time(world.hub.clock() - timedelta(seconds=HOUR)),
        "expires_at": format_time(world.hub.clock() + timedelta(hours=23)),
        "registered": False,
        "registered_user": None,
        "opened_at": None,
        "used_at": None,
        "used_by": None,
        "revoked_at": None,
    }
    world.hub.flush_read_log()
    assert [row[2] for row in audit(world, "read.org.invites")] == [UUID(draft.id)]


def test_list_shows_the_first_lookup_of_each_invitation(world: World) -> None:
    draft = world.public_org()
    first = create_invitation(draft)
    second = create_invitation(draft)
    other = world.public_org()
    foreign = create_invitation(other)

    def opened() -> dict[str, Any]:
        listed = draft.founder.browser.get(f"/v1/orgs/{draft.id}/invites").json()["invites"]
        return {item["invite_id"]: item["opened_at"] for item in listed}

    assert opened() == {first.invite_id: None, second.invite_id: None}
    world.hub.clock.advance(60)
    looked = format_time(world.hub.clock())
    assert world.hub.anon.post("/v1/invites/lookup", json={"invite_id": first.invite_id}).status_code == 200
    world.hub.clock.advance(60)
    assert world.hub.anon.post("/v1/invites/lookup", json={"invite_id": first.invite_id}).status_code == 200
    assert world.hub.anon.post("/v1/invites/lookup", json={"invite_id": foreign.invite_id}).status_code == 200
    assert opened() == {first.invite_id: looked, second.invite_id: None}


def test_list_is_for_admins_only(world: World) -> None:
    draft = world.public_org()
    member = world.user()
    world.join(draft, member)
    for client in (member.browser, world.user().browser):
        response = client.get(f"/v1/orgs/{draft.id}/invites")
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})


# --- revoke -----------------------------------------------------------------------------------------------------------


def test_revoke_appends_the_entry_and_deletes_the_package(world: World, published: MemoryPublisher) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    published.clear()
    world.hub.clock.advance(5)
    response = revoke_invitation(draft, invitation)
    assert (response.status_code, response.json()) == (200, {"seq": 2})
    row = invite_row(world, invitation.invite_id)
    assert (row[8], row[9]) == (world.hub.clock(), None)
    assert [row[4] for row in audit(world, "invite.revoke")] == [invitation.invite_id]
    assert [row[4] for row in audit(world, "log.invite_revoke")] == [invitation.invite_id]
    assert [item.frame for item in published.items()].count("log") == 1
    # the lookup no longer knows it (R-1.12)
    lookup = world.hub.anon.post("/v1/invites/lookup", json={"invite_id": invitation.invite_id})
    assert (lookup.status_code, lookup.json()) == (404, {"error": "invite_unknown"})
    # an identical repeat answers the same seq; another revocation is refused by the log rules
    again = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites/{invitation.invite_id}/revoke", json={"entry": draft.entries[-1]}
    )
    assert (again.status_code, again.json()) == (200, {"seq": 2})
    second = revoke_invitation(draft, invitation)
    assert (second.status_code, second.json()["error"]) == (400, "invite_revoked")


def test_revoke_of_a_used_invitation_is_refused(world: World, browser_accept: Any) -> None:
    draft = world.public_org()
    invitation, _user = browser_accept(draft)
    response = revoke_invitation(draft, invitation)
    assert (response.status_code, response.json()["error"]) == (400, "invite_consumed")


def test_revoke_refusals(world: World) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    # the entry names another invitation
    entry = draft.add("invite_revoke", draft.founder, invite_id=invitation.invite_id)
    path = f"/v1/orgs/{draft.id}/invites/{random_invite_id()[:21]}A/revoke"
    response = draft.founder.browser.post(path, json={"entry": entry})
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/entry/body/invite_id"]}},
    )
    draft.drop_pending()
    # an invitation of no org / of another org
    other = world.public_org(draft.founder)
    elsewhere = create_invitation(other)
    entry = draft.add("invite_revoke", draft.founder, invite_id=elsewhere.invite_id)
    response = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites/{elsewhere.invite_id}/revoke", json={"entry": entry}
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    draft.drop_pending()
    # a member is no admin
    member = world.user()
    world.join(draft, member)
    response = revoke_invitation(draft, invitation, by=member)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    # a wrong entry type
    entry = draft.add("invite_revoke", draft.founder, invite_id=invitation.invite_id)
    entry = {**entry, "body": {**entry["body"], "type": "remove"}}
    response = draft.founder.browser.post(
        f"/v1/orgs/{draft.id}/invites/{invitation.invite_id}/revoke", json={"entry": entry}, extensions=NO_CONTRACT
    )
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/entry"]}}


def test_invitation_id_of_a_platform_invitation_is_a_conflict(world: World) -> None:
    draft = world.public_org()
    taken = world.platform_invite(world.user())
    found = invite_format.invite_secrets(invite_format.generate_code())
    entry = draft.add(
        "invite",
        draft.founder,
        invite_id=taken,
        pk_inv=found.pk_inv,
        kind="user",
        role="member",
        wgs={},
        expires=expires_in(world, HOUR),
    )
    package = package_of(draft, found, [draft.default_wg])
    response = draft.founder.browser.post(f"/v1/orgs/{draft.id}/invites", json={"entry": entry, "package_ct": package})
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
