"""U-HUB-users (ADR-060, 04-crypto.md section 5.13): the admissible identity of an account through
`GET /v1/users/{user}/identity` and `GET /v1/users/lookup`, the caller's invitees (`GET /v1/me/invitees`) and the
operator's view (`GET /v1/platform/users/{user}`), against a platform log written through the hub."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest

from agent_hub.crypto import identity as identity_codes
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_hub.crypto.keys import Identity
from agent_hub.users.identity import invitees, proven_identity, registered_identity
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import Actor, World, insert_identity, insert_invite, new_id, new_invite_id
from tests.platform.hub.platform.conftest import Platform

Json = dict[str, Any]


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


@pytest.fixture
def plat(hub: HubHarness) -> Platform:
    return Platform(hub)


def _code(actor: Actor) -> str:
    return identity_codes.account_code(actor.id, actor.pub)


def _expected(actor: Actor, seq: int, id_sig: str = "") -> Json:
    return {"user_id": actor.id, "pub": actor.pub, "platform_seq": seq, "id_sig": id_sig, "account_code": _code(actor)}


def _certified(world: World, plat: Platform) -> tuple[Actor, int]:
    user = world.user()
    return user, plat.append("user_certify", user=user.id, pub=user.pub)


def _invited(world: World, plat: Platform, *, inviter: Actor | None = None, proof: bool = True) -> tuple[Actor, int]:
    """An account registered through a platform invitation: the ID derived from it and (with `proof`) the identity
    proof kept at registration."""
    found = invite_codes.invite_secrets(invite_codes.generate_code())
    seq = plat.append("user_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=plat.expires(72))
    body = cast(Json, plat.entries[seq]["body"])
    user_id = identity_codes.invited_user_id(found.invite_id, body["ts"])
    keys = Identity.generate()
    hub = world.hub
    insert_identity(hub.pool, user_id, keys, hub.clock())
    actor = Actor(user_id, "user", keys, hub.browser(uuid.UUID(user_id)))
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "UPDATE hub.invite SET registered_user = %s, registered_at = %s, created_by = %s WHERE id = %s",
            (user_id, hub.clock(), None if inviter is None else inviter.id, found.invite_id),
        )
        if proof:
            id_sig = identity_codes.sign_identity(found.signing, user_id, actor.pub)
            connection.execute(
                "UPDATE hub.user_identity SET id_sig = %s, id_invite_seq = %s WHERE user_id = %s",
                (b64u_decode(id_sig, 64), seq, user_id),
            )
    return actor, seq


def _proof(world: World, user: Actor) -> str:
    found = world.rows("SELECT id_sig FROM hub.user_identity WHERE user_id = %s", user.id)[0][0]
    return b64u_encode(bytes(found))


def _unproven(response: Any) -> None:
    assert (response.status_code, response.json()) == (404, {"error": "identity_unproven"})


# --- GET /v1/users/{user}/identity ---


def test_identity_of_a_certified_account(world: World, plat: Platform) -> None:
    user, seq = _certified(world, plat)
    response = world.user().browser.get(f"/v1/users/{user.id}/identity")
    assert (response.status_code, response.json()) == (200, _expected(user, seq))


def test_identity_of_an_account_with_an_identity_proof(world: World, plat: Platform) -> None:
    user, seq = _invited(world, plat)
    response = world.user().browser.get(f"/v1/users/{user.id}/identity")
    assert (response.status_code, response.json()) == (200, _expected(user, seq, _proof(world, user)))


def test_an_ended_certification_falls_back_to_the_identity_proof(world: World, plat: Platform) -> None:
    user, seq = _invited(world, plat)
    plat.append("user_certify", user=user.id, pub=user.pub)
    plat.append("user_decertify", user=user.id)
    assert world.user().browser.get(f"/v1/users/{user.id}/identity").json() == _expected(user, seq, _proof(world, user))


def test_a_certification_of_other_keys_does_not_anchor(world: World, plat: Platform) -> None:
    user = world.user()
    plat.append("user_certify", user=user.id, pub=Identity.generate().public())
    _unproven(world.user().browser.get(f"/v1/users/{user.id}/identity"))


def test_unknown_disabled_unanchored_and_malformed_accounts_are_unproven(world: World, plat: Platform) -> None:
    caller = world.user().browser
    certified, _ = _certified(world, plat)
    unanchored = world.user()
    forged, _ = _invited(world, plat)
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", (world.hub.clock(), certified.id))
        connection.execute(
            "UPDATE hub.user_identity SET id_sig = %s WHERE user_id = %s", (bytes(64), forged.id)
        )  # a proof that does not verify
    for user in (new_id(), certified.id, unanchored.id, forged.id):
        _unproven(caller.get(f"/v1/users/{user}/identity"))
    _unproven(caller.get("/v1/users/not-an-id/identity", extensions={"contract": False}))


def test_without_a_platform_log_no_account_is_admissible(world: World) -> None:
    user = world.user()
    _unproven(world.user().browser.get(f"/v1/users/{user.id}/identity"))
    _unproven(world.user().browser.get("/v1/users/lookup", params={"code": _code(user)}))
    with world.hub.pool.connection() as connection:
        assert proven_identity(connection, None, uuid.UUID(user.id)) is None
        assert invitees(connection, None, uuid.UUID(user.id)) == []


# --- GET /v1/users/lookup ---


def test_lookup_by_account_code(world: World, plat: Platform) -> None:
    certified, certified_seq = _certified(world, plat)
    invited, invited_seq = _invited(world, plat)
    forged, _ = _invited(world, plat)
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.user_identity SET id_sig = %s WHERE user_id = %s", (bytes(64), forged.id))
    caller = world.user().browser
    shown = identity_codes.format_account_code(_code(certified))
    assert caller.get("/v1/users/lookup", params={"code": shown}).json() == _expected(certified, certified_seq)
    typed = " ".join(_code(invited).lower()[i : i + 4] for i in (0, 4, 8))
    expected = _expected(invited, invited_seq, _proof(world, invited))
    assert caller.get("/v1/users/lookup", params={"code": typed}).json() == expected
    _unproven(caller.get("/v1/users/lookup", params={"code": _code(forged)}))
    _unproven(caller.get("/v1/users/lookup", params={"code": "AAAA-AAAA-AAAA"}))


def test_lookup_reads_the_look_alike_characters(world: World, plat: Platform) -> None:
    for _ in range(200):
        user, seq = _certified(world, plat)
        code = _code(user)
        if any(letter in code for letter in "OIB"):
            break
    typed = code.translate(str.maketrans("OIB", "018"))
    assert typed != code
    assert world.user().browser.get("/v1/users/lookup", params={"code": typed}).json() == _expected(user, seq)


@pytest.mark.parametrize("code", ["ABCD-EFGH-234", "AB9D-EFGH-2345", "ABCD-EFGH-23456"])
def test_lookup_with_an_invalid_code(world: World, code: str) -> None:
    response = world.user().browser.get("/v1/users/lookup", params={"code": code})
    assert (response.status_code, response.json()) == (400, {"error": "account_code_invalid"})


def test_lookup_without_a_code(world: World) -> None:
    response = world.user().browser.get("/v1/users/lookup", extensions={"contract": False})
    assert (response.status_code, response.json()) == (400, {"error": "account_code_invalid"})


# --- GET /v1/me/invitees ---


def test_invitees_are_the_accounts_of_the_own_platform_invitations(world: World, plat: Platform) -> None:
    inviter = world.user()
    first, first_seq = _invited(world, plat, inviter=inviter)
    world.hub.clock.advance(60)
    second, _ = _invited(world, plat, inviter=inviter, proof=False)
    _invited(world, plat, inviter=world.user())  # someone else's invitation
    hub = world.hub
    insert_invite(hub.pool, new_invite_id(), hub.clock())  # open, never registered
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.invite SET created_by = %s WHERE registered_user IS NULL", (inviter.id,))
    org = world.public_org(inviter)
    org_invite = new_invite_id()
    insert_invite(hub.pool, org_invite, hub.clock(), scope="org", org_id=org.id, registered_user=world.user().id)
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.invite SET created_by = %s WHERE id = %s", (inviter.id, org_invite))
    rows = dict(world.rows("SELECT registered_user, id FROM hub.invite WHERE registered_user IS NOT NULL"))
    response = inviter.browser.get("/v1/me/invitees")
    assert response.status_code == 200
    assert response.json() == {
        "invitees": [
            {
                "user_id": second.id,
                "invite_id": rows[uuid.UUID(second.id)],
                "registered_at": format_time(hub.clock()),
                "identity": None,
            },
            {
                "user_id": first.id,
                "invite_id": rows[uuid.UUID(first.id)],
                "registered_at": format_time(hub.clock() - timedelta(seconds=60)),
                "identity": _expected(first, first_seq, _proof(world, first)),
            },
        ]
    }
    assert world.user().browser.get("/v1/me/invitees").json() == {"invitees": []}
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", (hub.clock(), second.id))
    listed = [row["user_id"] for row in inviter.browser.get("/v1/me/invitees").json()["invitees"]]
    assert listed == [first.id]  # a disabled account is not disclosed (review finding L2 of KOR-37)


# --- GET /v1/platform/users/{user} ---


def test_platform_user_with_a_platform_invitation_and_a_certification(world: World, plat: Platform) -> None:
    user, _ = _invited(world, plat, proof=False)
    seq = plat.append("user_certify", user=user.id, pub=user.pub)
    invite_id = world.rows("SELECT id FROM hub.invite WHERE registered_user = %s", user.id)[0][0]
    response = world.hub.root.get(f"/v1/platform/users/{user.id}")
    assert (response.status_code, response.json()) == (
        200,
        {
            "user_id": user.id,
            "pub": user.pub,
            "account_code": _code(user),
            "created_at": format_time(world.hub.clock()),
            "platform_invite_id": invite_id,
            "registered_at": format_time(world.hub.clock()),
            "certified_seq": seq,
        },
    )
    plat.append("user_decertify", user=user.id)
    assert world.hub.root.get(f"/v1/platform/users/{user.id}").json()["certified_seq"] is None


def test_platform_user_without_a_platform_invitation(world: World, plat: Platform) -> None:
    user = world.user()
    found = world.hub.root.get(f"/v1/platform/users/{user.id}").json()
    assert (found["platform_invite_id"], found["registered_at"], found["certified_seq"]) == (None, None, None)
    plat.init()
    assert world.hub.root.get(f"/v1/platform/users/{user.id}").json() == found
    with world.hub.pool.connection() as connection:
        assert registered_identity(connection, None, uuid.UUID(user.id)) == found


def test_platform_user_unknown_or_disabled_is_not_found(world: World) -> None:
    disabled = world.user()
    with world.hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.principal SET disabled_at = %s WHERE id = %s", (world.hub.clock(), disabled.id))
    for user in (new_id(), disabled.id):
        response = world.hub.root.get(f"/v1/platform/users/{user}")
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})


# --- access and read log ---


def test_reads_are_logged_and_agents_may_read(world: World, plat: Platform) -> None:
    user, seq = _certified(world, plat)
    caller = world.user()
    assert caller.browser.get(f"/v1/users/{user.id}/identity").status_code == 200
    assert caller.browser.get("/v1/users/lookup", params={"code": _code(user)}).status_code == 200
    assert caller.browser.get("/v1/me/invitees").status_code == 200
    assert world.hub.root.get(f"/v1/platform/users/{user.id}").status_code == 200
    agent = agent_client(world.hub)
    assert agent.get(f"/v1/users/{user.id}/identity").json() == _expected(user, seq)
    world.hub.flush_read_log()
    logged = world.rows(
        "SELECT action, actor_id FROM hub.audit WHERE action IN ('read.user.identity', 'read.user.lookup',"
        " 'read.me.invitees', 'read.platform.user') ORDER BY seq"
    )
    actions = sorted(row[0] for row in logged)
    assert actions == [
        "read.me.invitees",
        "read.platform.user",
        "read.user.identity",
        "read.user.identity",
        "read.user.lookup",
    ]
    by_caller = {row[0] for row in logged if row[1] == uuid.UUID(caller.id)}
    assert by_caller == {"read.me.invitees", "read.user.identity", "read.user.lookup"}
    assert world.hub.anon.get(f"/v1/users/{user.id}/identity").json() == {"error": "signature_missing"}
