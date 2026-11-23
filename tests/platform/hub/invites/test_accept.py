"""U-HUB-invites (accept): using an org invitation (03-api.md sections 4.1-4.3, 04-crypto.md sections 4.7, 5.1, 5.2,
5.2a and 5.8, ADR-005, ADR-026; R-1.3, R-1.5, R-1.10, R-1.11, R-1.13).

Invitations are created through the API with a real package; users register with the fake authenticator and accept
through `POST /v1/orgs/{org}/log`, devices through their enrollment. The accept is single use (L7), refused after the
expiry (fake clock) and after a revocation, bound to the registered user (`invite_registered`), and an existing user
joins without a second identity. The used package is deleted and the lookup no longer knows the invitation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto import invite as invite_format
from agent_hub.crypto.keys import Identity
from agent_hub.errors import HubError
from agent_hub.invites import orgs as invite_orgs
from agent_hub.orgs.append import append_batch
from tests.platform.hub.auth.authenticator import FakeBrowser
from tests.platform.hub.device.support import Fleet, new_device
from tests.platform.hub.invites.support import HOUR, accept, create_invitation, registered_actor, revoke_invitation
from tests.platform.hub.orgs.support import Actor, World

AcceptFlow = Any


def invite_state(world: World, invite_id: str) -> tuple[Any, ...]:
    (row,) = world.rows("SELECT used_at, used_by, package_ct, registered_user FROM hub.invite WHERE id = %s", invite_id)
    return row


def lookup(world: World, invite_id: str) -> tuple[int, Any]:
    response = world.hub.anon.post("/v1/invites/lookup", json={"invite_id": invite_id})
    return response.status_code, response.json()


def members(world: World, org: str) -> list[tuple[str, str]]:
    rows = world.rows(
        "SELECT principal_id, role FROM hub.org_member WHERE org_id = %s AND removed_seq IS NULL ORDER BY joined_seq",
        org,
    )
    return [(str(principal), role) for principal, role in rows]


def test_new_user_registers_and_accepts(world: World, browser: FakeBrowser) -> None:
    # 04-crypto.md 5.1 + 5.2: lookup with package and log, registration bound to the invitation, accept.
    draft = world.public_org()
    invitation = create_invitation(draft, role="admin")
    status, found = lookup(world, invitation.invite_id)
    assert status == 200
    opened = invite_format.open_package(invitation.secrets, found["package_ct"])
    assert (opened["org"], found["log_head"]["seq"], found["registered"]) == (draft.id, 1, False)
    actor, _account = registered_actor(browser, invitation.invite_id)
    assert lookup(world, invitation.invite_id)[1]["registered"] is True
    world.hub.clock.advance(10)
    response = accept(draft, actor, invitation)
    assert (response.status_code, response.json()["head"]["seq"]) == (200, 2)
    used_at, used_by, package, registered = invite_state(world, invitation.invite_id)
    assert (used_at, str(used_by), package, str(registered)) == (world.hub.clock(), actor.id, None, actor.id)
    assert members(world, draft.id) == [(draft.founder.id, "admin"), (actor.id, "admin")]
    assert lookup(world, invitation.invite_id) == (404, {"error": "invite_unknown"})
    me = actor.browser.get("/v1/me").json()
    assert [org["org_id"] for org in me["orgs"]] == [draft.id]


def test_device_enrolls_with_an_invitation_of_the_api(world: World, fleet: Fleet) -> None:
    # R-3.2 (hub side): the device invitation created through the API is accepted by the enrollment.
    draft = world.public_org()
    invitation = create_invitation(draft, kind="device", seconds=24 * HOUR)
    device = new_device()
    response = fleet.enroll_org(draft, device, invitation.secrets)
    assert response.status_code == 200, response.text
    used_at, used_by, package, _registered = invite_state(world, invitation.invite_id)
    assert (used_at, str(used_by), package) == (world.hub.clock(), device.id, None)
    assert (device.id, "device") in members(world, draft.id)
    again = fleet.enroll_org(draft, new_device(), invitation.secrets)
    assert (again.status_code, again.json()) == (404, {"error": "invite_unknown"})


def test_an_invitation_is_used_once(world: World, browser_accept: AcceptFlow) -> None:
    # L7 / R-1.10: a second accept of the same invitation is refused, also by an existing user.
    draft = world.public_org()
    invitation, _first = browser_accept(draft)
    other = world.user()
    response = accept(draft, other, invitation)
    assert (response.status_code, response.json()["error"]) == (400, "invite_consumed")
    assert len(members(world, draft.id)) == 2


def test_expired_invitation_is_refused(world: World, browser: FakeBrowser) -> None:
    # R-1.11 with the fake clock: lookup and registration answer invite_unknown, the accept invite_expired.
    draft = world.public_org()
    invitation = create_invitation(draft, seconds=HOUR)
    actor, _account = registered_actor(browser, invitation.invite_id)
    late = create_invitation(draft, seconds=HOUR)
    world.hub.clock.advance(HOUR)
    assert lookup(world, invitation.invite_id) == (404, {"error": "invite_unknown"})
    begin = browser.register_begin(late.invite_id)
    assert (begin.status_code, begin.json()) == (404, {"error": "invite_unknown"})
    response = accept(draft, actor, invitation)
    assert (response.status_code, response.json()) == (410, {"error": "invite_expired"})
    assert invite_state(world, invitation.invite_id)[0] is None


def test_revoked_invitation_is_refused(world: World, browser: FakeBrowser) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    actor, _account = registered_actor(browser, invitation.invite_id)
    assert revoke_invitation(draft, invitation).status_code == 200
    response = accept(draft, actor, invitation)
    assert (response.status_code, response.json()["error"]) == (400, "invite_revoked")
    assert lookup(world, invitation.invite_id) == (404, {"error": "invite_unknown"})


def test_invitation_bound_to_another_registered_user(world: World, browser: FakeBrowser) -> None:
    # ADR-026: after a registration through the invitation only that user may accept it.
    draft = world.public_org()
    invitation = create_invitation(draft)
    registered_actor(browser, invitation.invite_id)
    existing = world.user()
    response = accept(draft, existing, invitation)
    assert (response.status_code, response.json()) == (409, {"error": "invite_registered"})
    assert invite_state(world, invitation.invite_id)[0] is None


def test_existing_user_joins_without_a_second_identity(world: World, browser: FakeBrowser) -> None:
    # R-1.13 / 04-crypto.md 5.2a: a user of another org accepts with the registered identity.
    first = world.public_org()
    invitation = create_invitation(first)
    actor, _account = registered_actor(browser, invitation.invite_id)
    assert accept(first, actor, invitation).status_code == 200
    second = world.public_org()
    further = create_invitation(second)
    identities = world.rows("SELECT count(*) FROM hub.user_identity")[0][0]
    response = accept(second, actor, further)
    assert response.status_code == 200, response.text
    assert world.rows("SELECT count(*) FROM hub.user_identity")[0][0] == identities
    assert (actor.id, "member") in members(world, second.id)
    _used, used_by, _package, registered = invite_state(world, further.invite_id)
    assert (str(used_by), registered) == (actor.id, None)
    me = actor.browser.get("/v1/me").json()
    assert sorted(org["org_id"] for org in me["orgs"]) == sorted([first.id, second.id])


def test_existing_user_must_use_the_registered_identity(world: World) -> None:
    draft = world.public_org()
    invitation = create_invitation(draft)
    existing = world.user()
    response = accept(draft, existing, invitation, identity=Identity.generate())
    assert (response.status_code, response.json()["error"]) == (400, "log_accept")


def test_a_user_cannot_take_a_device_invitation(world: World) -> None:
    # the browser route refuses a device `accept`; a user `accept` of a device invitation breaks the log rules
    draft = world.public_org()
    invitation = create_invitation(draft, kind="device", seconds=HOUR)
    actor: Actor = world.user()
    response = accept(draft, actor, invitation)
    assert (response.status_code, response.json()["error"]) == (400, "log_accept")


@pytest.mark.parametrize("attempt", range(3))
def test_revoke_racing_an_accept_never_deadlocks(world: World, attempt: int) -> None:
    # Both take the org row first (revoke: `orgs.lock_org`; accept: the append), so one wins and the other gets the
    # log refusal instead of a database deadlock.
    del attempt
    context = world.hub.app.state.hub
    draft = world.public_org()
    invitation = create_invitation(draft)
    invitee = world.user()
    seq = len(draft.entries)
    accept_entry = draft.accept(invitee, invitation.secrets)
    draft.entries.pop()
    revoke_entry = draft.add("invite_revoke", draft.founder, invite_id=invitation.invite_id)
    draft.entries.pop()
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def run(work: Callable[[Any], object]) -> None:
        barrier.wait()
        try:
            with context.pool.connection() as connection, connection.transaction():
                work(connection)
            outcomes.append("ok")
        except HubError as error:
            outcomes.append(error.code)

    def do_accept(connection: Any) -> object:
        return append_batch(
            connection,
            org_id=UUID(draft.id),
            appender=invitee.appender,
            expected_seq=seq,
            entries=[accept_entry],
            wraps=[],
            events=[],
            now=world.hub.clock(),
            root_key=world.hub.config.platform_root_key,
        )

    def do_revoke(connection: Any) -> object:
        return invite_orgs.revoke(
            connection,
            org_id=UUID(draft.id),
            user=UUID(draft.founder.id),
            invite_id=invitation.invite_id,
            entry=revoke_entry,
            now=world.hub.clock(),
            root_key=world.hub.config.platform_root_key,
        )

    threads = [threading.Thread(target=run, args=(work,)) for work in (do_accept, do_revoke)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ok", "seq_conflict"], outcomes
