"""U-HUB-agents (own identity, ADR-082 sections 1, 2, 5 and 9; 11-agent-access.md section 2): the confirmation hands
over no identity - the certificate of the generation the hub counts, the user's anchor and one batch per organization
of the user are stored in one transaction, or nothing is; the old form is `client_outdated` for a browser; an agent
that still holds the identity converts itself; an own-identity agent acts under its own ID with its user's rights."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_hub.agents import store
from agent_hub.agents.identity import actor, audience_of
from agent_hub.auth.keys import Principal
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import Identity
from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from agent_hub.errors import HubError
from agent_hub.log.entries import sign_log_entry
from agent_hub.orgs import logs
from agent_hub.orgs.append import Appender, authored, removed
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.agents.own import (
    OwnAgent,
    add_batch,
    agent_actor,
    confirm_body,
    own_agent,
    paired,
    submitted,
    user_uuid,
)
from tests.platform.hub.agents.support import (
    agent_row,
    certificate,
    gesture_client,
    handover,
    rows,
    stored_agent,
)
from tests.platform.hub.agents.test_accounts import register
from tests.platform.hub.auth.authenticator import seed_invite
from tests.platform.hub.conftest import HubDatabase, HubHarness, uuid7
from tests.platform.hub.orgs.support import Actor, OrgDraft, World, new_id

NO_CONTRACT = {"contract": False}
MIGRATIONS = Path(migrate.__file__).with_name("migrations")
AT = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


def two_orgs(world: World) -> tuple[Actor, OrgDraft, OrgDraft]:
    """A user who founded two organizations (no other member)."""
    first = world.public_org()
    second = world.public_org(first.founder)
    return first.founder, first, second


def heads(world: World) -> list[tuple[Any, ...]]:
    return world.rows("SELECT id, log_head_seq FROM hub.org ORDER BY id")


# --- the generation ---------------------------------------------------------------------------------------------------


def test_the_pairing_names_the_generation_to_a_signed_user(world: World) -> None:
    hub = world.hub
    user = world.user()
    agent = paired(hub)
    assert "gen" not in hub.anon.get(f"/v1/agent-pairings/{agent.agent_id}").json()
    assert user.browser.get(f"/v1/agent-pairings/{agent.agent_id}").json()["gen"] == 1
    own = own_agent(hub, user, [])
    assert user.browser.get(f"/v1/agent-pairings/{paired(hub).agent_id}").json()["gen"] == 2
    # a revoked agent keeps its generation counted; an agent of the user is told the same
    assert user.browser.delete(f"/v1/me/agents/{own.agent.agent_id}").status_code == 200
    first = stored_agent(hub, user_uuid(user))
    assert first.client(hub).get(f"/v1/agent-pairings/{paired(hub).agent_id}").json()["gen"] == 2


# --- the confirmation in the new form ---------------------------------------------------------------------------------


def test_the_confirmation_adds_the_agent_to_every_log_in_one_transaction(
    world: World, published: MemoryPublisher
) -> None:
    hub = world.hub
    user, first, second = two_orgs(world)
    agent = paired(hub)
    confirm = confirm_body(hub, agent, user, [second, first])  # any order: the hub locks the orgs sorted
    session = gesture_client(hub, user_uuid(user), agent.agent_id)
    published.clear()
    response = session.post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(agent.agent_id)})
    submitted(first, second)
    for draft in (first, second):
        found = world.rows(
            "SELECT type, by_id, subject_id FROM hub.membership_entry WHERE org_id = %s AND seq = 1", draft.id
        )
        assert found == [("agent_add", uuid.UUID(user.id), agent.agent_id)]
        wraps = world.rows(
            "SELECT scope, epoch, wrapped_by FROM hub.key_wrap WHERE recipient_id = %s AND scope_id = ANY(%s::uuid[])"
            " ORDER BY scope",
            agent.agent_id,
            [draft.id, draft.default_wg],
        )
        assert wraps == [("org", 1, uuid.UUID(user.id)), ("wg", 1, uuid.UUID(user.id))]
        # the agent is a log principal, never a member row with route rights of its own
        assert world.rows("SELECT count(*) FROM hub.org_member WHERE principal_id = %s", agent.agent_id) == [(0,)]
    audit = rows(hub, "SELECT action, actor_kind, actor_id FROM hub.audit WHERE action = 'log.agent_add' ORDER BY seq")
    assert audit == [("log.agent_add", "user", uuid.UUID(user.id))] * 2
    frames = [(item.frame, item.audience) for item in published.items() if item.frame in ("log", "wraps")]
    assert sorted(frames, key=str) == sorted(
        [
            ("log", Audience("org", uuid.UUID(first.id))),
            ("log", Audience("org", uuid.UUID(second.id))),
            ("wraps", Audience("user", uuid.UUID(user.id))),
            ("wraps", Audience("user", uuid.UUID(user.id))),
        ],
        key=str,
    )
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["own_identity"], row["held_identity"], row["gen"]) == (True, False, 1)
    listed = session.get("/v1/me/agents").json()["agents"]
    assert [(item["own_identity"], item["held_identity"], item["gen"], item["revoked_reason"]) for item in listed] == [
        (True, False, 1, None)
    ]
    # what the user's clients need to add the agent to a log that does not name it yet (ADR-082 section 4)
    assert (listed[0]["pub"], listed[0]["cert"], listed[0]["session_id"]) == (
        agent.identity.public(),
        confirm.body["cert"],
        None,
    )
    # a repeat after a lost answer changes nothing (the batches are not appended again)
    assert session.post("/v1/me/agents", json=confirm.body).json() == {"agent_id": str(agent.agent_id)}
    assert heads(world) == sorted([(uuid.UUID(first.id), 1), (uuid.UUID(second.id), 1)])


def test_a_missing_or_unexpected_batch_changes_nothing(world: World) -> None:
    hub = world.hub
    user, first, second = two_orgs(world)
    stranger = world.public_org()
    before = heads(world)
    agent = paired(hub)
    session = gesture_client(hub, user_uuid(user), agent.agent_id)
    missing = confirm_body(hub, agent, user, [first])
    response = session.post("/v1/me/agents", json=missing.body)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "agent_batches_incomplete", "detail": {"orgs": [second.id]}},
    )
    first.drop_pending()
    stranger_batch = add_batch(stranger, stranger.founder, agent)
    stranger.drop_pending()
    unexpected = confirm_body(hub, agent, user, [first, second])
    body = {**unexpected.body, "batches": [*unexpected.body["batches"], stranger_batch]}
    response = session.post("/v1/me/agents", json=body)
    assert response.json() == {"error": "agent_batches_incomplete", "detail": {"orgs": [stranger.id]}}
    twice = {**unexpected.body, "batches": [unexpected.body["batches"][0]] * 2}
    response = session.post("/v1/me/agents", json=twice, extensions=NO_CONTRACT)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/batches"]}}
    first.drop_pending()
    second.drop_pending()
    assert heads(world) == before
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert row["state"] == "open"
    # nothing of the refused batches was cached (E-19)
    with hub.pool.connection() as connection:
        for draft in (first, second):
            org = logs.org_row(connection, uuid.UUID(draft.id))
            assert org is not None
            assert logs.verified_log(connection, org, None).head.seq == 0


def test_a_batch_without_exactly_this_agent_by_another_principal_or_forged_is_refused(world: World) -> None:
    hub = world.hub
    draft = world.public_org()
    user = draft.founder
    agent = paired(hub)
    session = gesture_client(hub, user_uuid(user), agent.agent_id)
    body = handover(agent, user.id, user.identity).body
    # the batch adds the agent with another generation than the confirmation names
    batch = add_batch(draft, user, agent, gen=2)
    draft.drop_pending()
    response = session.post("/v1/me/agents", json={**body, "batches": [batch]})
    assert (response.status_code, response.json()) == (
        409,
        {"error": "agent_batches_incomplete", "detail": {"orgs": [draft.id]}},
    )
    # a certificate the user never signed (a hub inserting a sub-principal) is refused by the log rules
    batch = add_batch(draft, user, agent, cert=certificate(agent, user.id, Identity.generate(), 1))
    draft.drop_pending()
    response = session.post("/v1/me/agents", json={**body, "batches": [batch]})
    assert (response.status_code, response.json()) == (400, {"error": "log_agent"})
    # an entry by another principal (here the agent itself) is never part of the user's confirmation
    draft.add(
        "agent_add",
        agent_actor(hub, agent),
        subject=str(agent.agent_id),
        user=user.id,
        pub=agent.identity.public(),
        gen=1,
        cert=body["cert"],
    )
    batch = {"org": draft.id, "expected_seq": 1, "entries": draft.pending(), "wraps": []}
    draft.drop_pending()
    response = session.post("/v1/me/agents", json={**body, "batches": [batch]})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    # a stale head is the ordinary seq_conflict (the client retries with a fresh head, ADR-082 section 9.12)
    batch = {**add_batch(draft, user, agent), "expected_seq": 5}
    draft.drop_pending()
    response = session.post("/v1/me/agents", json={**body, "batches": [batch]})
    assert response.json() == {"error": "seq_conflict", "detail": {"expected_seq": 1}}
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert row["state"] == "open"
    assert heads(world) == [(uuid.UUID(draft.id), 0)]


def test_the_generation_and_the_certificate_are_checked(world: World) -> None:
    hub = world.hub
    user = world.user()
    agent = paired(hub)
    session = gesture_client(hub, user_uuid(user), agent.agent_id)
    response = session.post("/v1/me/agents", json=handover(agent, user.id, user.identity, gen=2).body)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "agent_gen_mismatch", "detail": {"expected": 1}},
    )
    other = {
        **handover(agent, user.id, user.identity).body,
        "cert": certificate(agent, user.id, Identity.generate(), 1),
    }
    response = session.post("/v1/me/agents", json=other)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    # a user without a registered identity has no key to check a certificate under
    bare = uuid7(hub.clock())
    stranger = paired(hub)
    body = handover(stranger, bare, Identity.generate()).body
    response = gesture_client(hub, bare, stranger.agent_id).post("/v1/me/agents", json=body)
    assert response.json() == {"error": "signature_invalid"}


def test_the_old_form_is_outdated_for_a_browser_and_the_forms_never_mix(world: World) -> None:
    hub = world.hub
    user = world.user()
    agent = paired(hub)
    session = gesture_client(hub, user_uuid(user), agent.agent_id)
    old = handover(agent, user.id, user.identity, old=True).body
    response = session.post("/v1/me/agents", json=old)
    assert (response.status_code, response.json()) == (409, {"error": "client_outdated"})
    new = handover(agent, user.id, user.identity).body
    for body in (
        {**new, "bundle_ct": old["bundle_ct"]},
        {key: value for key, value in new.items() if key != "batches"},
        {**old, "cred_wraps": []},
        {key: value for key, value in old.items() if key != "bundle_ct"},
    ):
        response = session.post("/v1/me/agents", json=body, extensions=NO_CONTRACT)
        assert (response.status_code, response.json()) == (
            400,
            {"error": "request_invalid", "detail": {"pointers": ["/bundle_ct"]}},
        )
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert row["state"] == "open"


def test_an_own_identity_agent_never_confirms(world: World) -> None:
    hub = world.hub
    user = world.user()
    own = own_agent(hub, user, [])
    successor = paired(hub)
    body = handover(successor, user.id, user.identity, gen=2).body
    response = own.agent.client(hub).post("/v1/me/agents", json=body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


def test_the_agent_of_an_account_alone_confirms_its_successor_in_the_new_form(hub: HubHarness) -> None:
    account = register(hub, seed_invite(hub))
    successor = paired(hub)
    confirm = handover(successor, account.user_id, account.identity)
    response = account.client(hub).post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(successor.agent_id)})
    successor.user_id = uuid.UUID(account.user_id)
    claimed = successor.client(hub).post("/v1/agent/claim", json=confirm.claim_body).json()
    assert (claimed["identity"], claimed["gen"], claimed["migrate"]) == ("own", 1, False)
    assert "bundle_ct" not in claimed
    # the registering agent held the account's identity: it is no candidate for a conversion (ADR-082 section 7)
    previous = agent_row(hub, account.agent.agent_id)
    assert previous is not None
    assert (previous["state"], previous["revoked_reason"], previous["held_identity"]) == ("revoked", "replaced", True)


# --- the own-identity agent at work -----------------------------------------------------------------------------------


def scene_agent(world: World) -> tuple[OrgDraft, Actor, OwnAgent]:
    """An org of a founder with a second member and an own-identity agent of the founder."""
    draft = world.public_org()
    member = world.user()
    world.join(draft, member)
    return draft, member, own_agent(world.hub, draft.founder, [draft])


def test_the_agent_appends_wraps_and_reads_under_its_own_id(world: World, published: MemoryPublisher) -> None:
    hub = world.hub
    draft, member, own = scene_agent(world)
    user, agent = own.user, own.actor
    client = own.agent.client(hub)
    me = client.get("/v1/agent/me").json()
    assert (me["identity"], me["gen"], me["migrate"]) == ("own", 1, False)
    # its own wraps (recipient = the agent ID)
    listed = client.get("/v1/wraps").json()["wraps"]
    assert {(item["scope"], item["recipient_id"], item["wrapped_by"]) for item in listed} == {
        ("org", agent.id, user.id),
        ("wg", agent.id, user.id),
    }
    # entries by itself with its user's rights; the audit names the user and the agent
    wg = new_id()
    draft.add("wg_create", agent, wg=wg, epoch=1)
    published.clear()
    response = draft.post(agent, draft.wraps(agent, "wg", wg, 1, [user, agent]), client=client)
    assert response.status_code == 200, response.text
    assert world.rows("SELECT by_id FROM hub.membership_entry WHERE type = 'wg_create'") == [(uuid.UUID(agent.id),)]
    assert rows(hub, "SELECT actor_kind, actor_id, agent_id FROM hub.audit WHERE action = 'log.wg_create'") == [
        ("user", uuid.UUID(user.id), own.agent.agent_id)
    ]
    assert world.rows("SELECT DISTINCT wrapped_by FROM hub.key_wrap WHERE scope_id = %s", wg) == [
        (uuid.UUID(agent.id),)
    ]
    wraps_frames = sorted(item.audience.id for item in published.items() if item.frame == "wraps")
    # the wraps to the user and to the agent: both reach the user's channel
    assert wraps_frames == [uuid.UUID(user.id)] * 2
    # an entry by its user is not the agent's to submit
    draft.add("wg_create", user, wg=new_id(), epoch=1)
    response = draft.post(agent, client=client)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    draft.drop_pending()
    # events under its own ID: stored with author kind agent, audited as its user
    event = draft.event(agent, "member.profile", ref=user.id)
    response = client.post("/v1/events", json={"events": [{"event": event}]})
    assert response.status_code == 200, response.text
    stored = world.rows("SELECT author_id, author_kind FROM hub.event WHERE id = %s", event["header"]["id"])
    assert stored == [(uuid.UUID(agent.id), "agent")]
    audited = rows(hub, "SELECT actor_kind, actor_id FROM hub.audit WHERE event_id = %s", event["header"]["id"])
    assert audited == [("user", uuid.UUID(user.id))]
    foreign = draft.event(user, "member.profile", ref=user.id)
    response = client.post("/v1/events", json={"events": [{"event": foreign}]})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    # wraps through POST /v1/wraps as itself, and the catch-up it may fill
    missing = client.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    # the new workgroup is read by the user and its agent only, both hold their wraps
    assert [slot for slot in missing if slot["scope_id"] == wg] == []
    draft.add("grant", agent, subject=member.id, wg=wg, access="read")
    assert draft.post(agent, client=client).status_code == 200
    slot = {"scope": "wg", "scope_id": wg, "epoch": 1, "recipient_id": member.id}
    assert slot in client.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]
    published.clear()
    stored_wraps = client.post("/v1/wraps", json={"wraps": [draft.wrap(agent, "wg", wg, 1, member)]})
    assert stored_wraps.json() == {"stored": 1, "ignored": 0}
    assert world.rows(
        "SELECT wrapped_by FROM hub.key_wrap WHERE scope_id = %s AND recipient_id = %s", wg, member.id
    ) == [(uuid.UUID(agent.id),)]
    assert [item.audience for item in published.items() if item.frame == "wraps"] == [
        Audience("user", uuid.UUID(member.id))
    ]
    assert slot not in client.get(f"/v1/orgs/{draft.id}/catch-up").json()["missing"]


def test_a_higher_generation_replaces_the_agent_in_the_log_and_owes_its_rotation(world: World) -> None:
    hub = world.hub
    draft, member, first = scene_agent(world)
    user = first.user
    successor = paired(hub)
    session = gesture_client(hub, user_uuid(user), successor.agent_id)
    # the implicit removal of the old agent owes the rotation of everything it read: refused without it
    batch = add_batch(draft, user, successor, gen=2)
    draft.drop_pending()
    body = {**handover(successor, user.id, user.identity, gen=2).body, "batches": [batch]}
    response = session.post("/v1/me/agents", json=body)
    assert (response.status_code, response.json()["error"]) == (409, "rotation_required")
    # with the rotations and the new epochs for every remaining reader it is accepted
    recipient = agent_actor(hub, successor)
    readers = [user, member]
    draft.add(
        "agent_add",
        user,
        subject=str(successor.agent_id),
        user=user.id,
        pub=successor.identity.public(),
        gen=2,
        cert=certificate(successor, user.id, user.identity, 2),
    )
    draft.add("rotate", user, scope="org", scope_id=draft.id, epoch=2)
    draft.add("rotate", user, scope="wg", scope_id=draft.default_wg, epoch=2)
    wraps = [
        *[draft.wrap(user, "org", draft.id, 2, reader) for reader in [*readers, recipient]],
        *[draft.wrap(user, "wg", draft.default_wg, 2, reader) for reader in [*readers, recipient]],
        draft.wrap(user, "org", draft.id, 1, recipient),
        draft.wrap(user, "wg", draft.default_wg, 1, recipient),
    ]
    batch = {"org": draft.id, "expected_seq": draft.submitted, "entries": draft.pending(), "wraps": wraps}
    response = session.post("/v1/me/agents", json={**body, "batches": [batch]})
    assert response.status_code == 200, response.text
    submitted(draft)
    old = agent_row(hub, first.agent.agent_id)
    assert old is not None
    assert (old["state"], old["revoked_reason"], old["user_ct"]) == ("revoked", "replaced", None)
    # the replaced agent is out of the log and its wraps of the org's scopes are deleted
    with hub.pool.connection() as connection:
        org = logs.org_row(connection, uuid.UUID(draft.id))
        assert org is not None
        state = logs.verified_log(connection, org, None).state
    assert state.member(first.actor.id) is None
    assert state.member(str(successor.agent_id)) is not None
    assert world.rows("SELECT count(*) FROM hub.key_wrap WHERE recipient_id = %s", first.actor.id) == [(0,)]


def test_a_member_without_admin_right_replaces_its_agent_with_the_rotations_it_owes(world: World) -> None:
    """ADR-082 section 9.1 and `OrgState.agent_owed`: the scopes owed by replacing its own agent are rotated by the
    user even without the admin role."""
    hub = world.hub
    draft = world.public_org()
    admin, member = draft.founder, world.user()
    world.join(draft, member)
    first = own_agent(hub, member, [draft])
    successor = paired(hub)
    recipient = agent_actor(hub, successor)
    draft.add(
        "agent_add",
        member,
        subject=str(successor.agent_id),
        user=member.id,
        pub=successor.identity.public(),
        gen=2,
        cert=certificate(successor, member.id, member.identity, 2),
    )
    draft.add("rotate", member, scope="org", scope_id=draft.id, epoch=2)
    draft.add("rotate", member, scope="wg", scope_id=draft.default_wg, epoch=2)
    readers = [admin, member, recipient]
    wraps = [
        *[draft.wrap(member, "org", draft.id, 2, reader) for reader in readers],
        *[draft.wrap(member, "wg", draft.default_wg, 2, reader) for reader in readers],
    ]
    batch = {"org": draft.id, "expected_seq": draft.submitted, "entries": draft.pending(), "wraps": wraps}
    body = {**handover(successor, member.id, member.identity, gen=2).body, "batches": [batch]}
    response = gesture_client(hub, user_uuid(member), successor.agent_id).post("/v1/me/agents", json=body)
    assert response.status_code == 200, response.text
    submitted(draft)
    with hub.pool.connection() as connection:
        org = logs.org_row(connection, uuid.UUID(draft.id))
        assert org is not None
        state = logs.verified_log(connection, org, None).state
    assert (state.member(first.actor.id), state.member(recipient.id) is not None) == (None, True)
    assert state.epoch("org", draft.id) == 2
    assert world.rows("SELECT role FROM hub.org_member WHERE principal_id = %s", member.id) == [("member",)]


# --- the conversion of an agent that holds its user's identity --------------------------------------------------------


def holding_agent(world: World) -> tuple[OrgDraft, Any]:
    """An org of a user with a passkey and an agent of the user paired before ADR-082 (it holds the identity)."""
    draft = world.public_org()
    agent = stored_agent(world.hub, user_uuid(draft.founder))
    return draft, agent


def test_an_agent_holding_the_identity_converts_itself(world: World, published: MemoryPublisher) -> None:
    hub = world.hub
    draft, agent = holding_agent(world)
    user = draft.founder
    client = agent.client(hub)
    me = client.get("/v1/agent/me").json()
    assert (me["identity"], me["migrate"], me["migrate_gen"]) == ("user", True, 1)
    assert "user_ct" not in me
    # the entries are by the user, signed with the identity the agent still holds
    confirm = confirm_body(hub, agent, user, [draft])
    body = {key: confirm.body[key] for key in ("gen", "cert", "user_ct", "batches", "cred_wraps")}
    published.clear()
    response = client.post("/v1/agent/identity", json=body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(agent.agent_id), "gen": 1})
    submitted(draft)
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["own_identity"], row["held_identity"], row["bundle_ct"], row["gen"]) == (True, True, None, 1)
    assert [item.frame for item in published.items() if item.frame == "log"] == ["log"]
    audit = rows(hub, "SELECT actor_id, target_id, detail, agent_id FROM hub.audit WHERE action = 'agent.identity'")
    assert audit == [
        (uuid.UUID(user.id), str(agent.agent_id), {"agent": str(agent.agent_id), "gen": 1}, agent.agent_id)
    ]
    me = client.get("/v1/agent/me").json()
    assert (me["identity"], me["gen"], me["migrate"], me["user_ct"]) == ("own", 1, False, body["user_ct"])
    assert "bundle_ct" not in me
    # from now on it acts under its own ID
    wg = new_id()
    actor_of = agent_actor(hub, agent)
    draft.add("wg_create", actor_of, wg=wg, epoch=1)
    assert draft.post(actor_of, draft.wraps(actor_of, "wg", wg, 1, [user, actor_of]), client=client).status_code == 200
    # a repeat after a lost answer is answered alike; other values are a conflict
    assert client.post("/v1/agent/identity", json=body).json() == {"agent_id": str(agent.agent_id), "gen": 1}
    other = {**body, "user_ct": b64u_encode(bytes(64))}
    response = client.post("/v1/agent/identity", json=other)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    listed = user.browser.get("/v1/me/agents").json()["agents"]
    assert [(item["own_identity"], item["held_identity"]) for item in listed] == [(True, True)]


def test_the_conversion_is_checked_like_a_confirmation(world: World) -> None:
    hub = world.hub
    draft, agent = holding_agent(world)
    user = draft.founder
    client = agent.client(hub)
    body = {key: value for key, value in handover(agent, user.id, user.identity).body.items() if key in OWN_KEYS}
    response = client.post("/v1/agent/identity", json=body)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "agent_batches_incomplete", "detail": {"orgs": [draft.id]}},
    )
    response = client.post("/v1/agent/identity", json={**body, "gen": 2})
    assert response.json() == {"error": "agent_gen_mismatch", "detail": {"expected": 1}}
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["own_identity"], row["bundle_ct"] is not None) == (False, True)
    # the registering agent of an account alone holds the account's identity by design (ADR-082 section 7)
    account = register(hub, seed_invite(hub))
    me = account.client(hub).get("/v1/agent/me").json()
    assert (me["identity"], me["migrate"]) == ("user", False)
    assert "migrate_gen" not in me
    alone = handover(account.agent, account.user_id, account.identity).body
    response = account.client(hub).post("/v1/agent/identity", json={key: alone[key] for key in OWN_KEYS})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    # a row revoked while the request ran
    own = store.OwnIdentity(1, bytes(64), bytes(64))
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.agent SET state = 'revoked', revoked_at = now(), bundle_ct = NULL WHERE id = %s",
            (agent.agent_id,),
        )
        with pytest.raises(HubError) as refused:
            store.convert(connection, agent.agent_id, user_uuid(user), own, hub.clock())
    assert refused.value.code == "key_revoked"


OWN_KEYS = ("gen", "cert", "user_ct", "batches", "cred_wraps")


# --- an organization created or joined by the agent -------------------------------------------------------------------


def test_the_agent_founds_an_org_of_its_user_and_accepts_an_invitation_for_it(world: World) -> None:
    hub = world.hub
    user = world.user()
    own = own_agent(hub, user, [])
    agent = own.actor
    client = own.agent.client(hub)
    member = {"id": agent.id, "pub": own.agent.identity.public(), "gen": 1, "cert": own.confirm.body["cert"]}
    draft = world.org(user)
    draft.add(
        "genesis",
        user,
        signer=own.agent.identity,
        subject=user.id,
        pub=user.pub,
        default_wg=draft.default_wg,
        private=False,
        agent=member,
    )
    body = {
        "genesis": draft.entries[0],
        "wraps": [
            draft.wrap(agent, "org", draft.id, 1, user),
            draft.wrap(agent, "org", draft.id, 1, agent),
            draft.wrap(agent, "wg", draft.default_wg, 1, user),
            draft.wrap(agent, "wg", draft.default_wg, 1, agent),
        ],
        "events": [
            draft.event(agent, "org.profile", lh=0),
            draft.event(agent, "wg.profile", lh=0, wg=draft.default_wg),
            draft.event(agent, "member.profile", lh=0, ref=user.id),
        ],
    }
    response = client.post("/v1/orgs", json=body)
    assert response.status_code == 200, response.text
    draft.submitted = 1
    assert world.rows("SELECT created_by FROM hub.org WHERE id = %s", draft.id) == [(uuid.UUID(user.id),)]
    assert rows(hub, "SELECT actor_id, agent_id FROM hub.audit WHERE action = 'log.genesis'") == [
        (uuid.UUID(user.id), own.agent.agent_id)
    ]
    # a genesis that carries another agent is not this agent's
    stranger = world.org(user)
    stranger.add(
        "genesis",
        user,
        signer=own.agent.identity,
        subject=user.id,
        pub=user.pub,
        default_wg=stranger.default_wg,
        private=False,
        agent={**member, "id": str(uuid7(hub.clock()))},
    )
    refused = client.post("/v1/orgs", json={**body, "genesis": stranger.entries[0]})
    assert (refused.status_code, refused.json()) == (403, {"error": "forbidden"})
    # an invitation of another org, accepted by the agent for its user (the agent joins with its user)
    other = world.public_org()
    found = other.invite(other.founder)
    other.append(other.founder)
    accept = other.body(
        "accept", user, invite_id=found.invite_id, subject=user.id, kind="user", pub=user.pub, agent=member
    )
    other.entries.append(sign_log_entry(accept, own.agent.identity.sig, found.signing))
    response = other.post(agent, client=client)
    assert response.status_code == 200, response.text
    accepted = "SELECT by_id, subject_id FROM hub.membership_entry WHERE org_id = %s AND type = 'accept'"
    joined = world.rows(accepted, other.id)
    assert joined == [(uuid.UUID(user.id), uuid.UUID(user.id))]
    assert rows(hub, "SELECT actor_id, agent_id FROM hub.audit WHERE action = 'log.accept'") == [
        (uuid.UUID(user.id), own.agent.agent_id)
    ]


# --- units ------------------------------------------------------------------------------------------------------------


def test_the_cryptographic_actor_of_a_request() -> None:
    user, agent, device = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = Principal(kind="session", key_id="session.x", principal_id=user, actor_kind="user")
    assert actor(session) == Appender(str(user), "user")
    holding = Principal(
        kind="agent", key_id="agent.x", principal_id=user, actor_kind="user", attributes={"agent_id": agent}
    )
    assert actor(holding) == Appender(str(user), "user")
    own = Principal(
        kind="agent",
        key_id="agent.x",
        principal_id=user,
        actor_kind="user",
        attributes={"agent_id": agent, "own_identity": True},
    )
    found = actor(own)
    assert (found, found.actor_kind, found.actor_id) == (Appender(str(agent), "agent", str(user)), "user", str(user))
    machine = Principal(kind="device", key_id="device.x", principal_id=device, actor_kind="device")
    assert (actor(machine).actor_kind, actor(machine).actor_id) == ("device", str(device))
    assert Appender(str(agent), "agent").actor_id == str(agent)
    genesis = {"type": "genesis", "by": str(user), "agent": {"id": str(agent)}}
    assert authored(genesis, found)
    assert not authored({**genesis, "agent": "x"}, found)
    assert not authored({**genesis, "type": "grant"}, found)
    assert not authored(genesis, Appender(str(device), "user"))


def logs_state_unused() -> Any:
    """A placeholder state (`removed` returns before looking at it for a genesis)."""
    return None


def test_removed_principals_and_audiences(world: World) -> None:
    hub = world.hub
    assert removed(None, logs_state_unused()) == []
    user = world.user()
    own = own_agent(hub, user, [])
    with hub.pool.connection() as connection:
        assert audience_of(connection, own.actor.id, None) == Audience("user", uuid.UUID(user.id))
        assert audience_of(connection, user.id, "user") == Audience("user", uuid.UUID(user.id))
        stray = str(uuid.uuid4())
        assert audience_of(connection, stray, None) == Audience("user", uuid.UUID(stray))
        with pytest.raises(ValueError, match="reason"):
            store.revoke(connection, user_uuid(user), own.agent.agent_id, hub.clock(), reason="bored")
        with pytest.raises(HubError) as refused:
            store.agent_author_key(connection, user_uuid(user), uuid.uuid4())
        assert refused.value.code == "forbidden"
        assert not store.cert_valid(
            connection, uuid.uuid4(), own.agent.agent_id, (bytes(32), bytes(32)), gen=1, cert=bytes(64)
        )


# --- migration 0026 ---------------------------------------------------------------------------------------------------


def test_0026_applies_on_the_schema_of_0025_with_data(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql") if path.name < "0026")
    for name in names:
        shutil.copy(MIGRATIONS / name, previous / name)
    assert run_migrate(hub_db.migrate_env(), directory=previous) == (f"applied {len(names)}", 0)
    user, agent_id = uuid.uuid4(), uuid.uuid4()
    insert = (
        "INSERT INTO hub.agent (id, user_id, kem_pub, sig_pub, state, bundle_ct, label_ct, claim_hash, created_at,"
        " expires_at, confirmed_at, claimed_at) VALUES (%s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)"
    )
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (user, AT))
        writer.execute(
            insert,
            (agent_id, user, bytes(32), b"\x01" * 32, bytes(64), bytes(64), bytes(32), AT, AT.replace(hour=11), AT, AT),
        )
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        found = writer.execute(
            "SELECT own_identity, held_identity, gen, cert, user_ct, session_id, revoked_reason FROM hub.agent"
        ).fetchall()
        assert found == [(False, True, None, None, None, None, None)]
        refused = (
            # a confirmed or active row holds a sealed identity or a sealed anchor
            "UPDATE hub.agent SET bundle_ct = NULL",
            # an own-identity row has a certificate and a generation and no sealed identity
            "UPDATE hub.agent SET own_identity = true",
            "UPDATE hub.agent SET own_identity = true, cert = '\\x00'::bytea",
            "UPDATE hub.agent SET user_ct = decode(repeat('00', 48), 'hex')",
            # a reason only for a revoked row
            "UPDATE hub.agent SET revoked_reason = 'revoked'",
            "UPDATE hub.agent SET gen = 0",
        )
        for statement in refused:
            with pytest.raises(psycopg.errors.CheckViolation):
                writer.execute(statement)
        writer.execute(
            "UPDATE hub.agent SET own_identity = true, gen = 1, cert = %s, user_ct = %s, bundle_ct = NULL",
            (bytes(64), bytes(64)),
        )
        writer.execute(
            "UPDATE hub.agent SET state = 'revoked', revoked_at = %s, revoked_reason = 'replaced', user_ct = NULL",
            (AT,),
        )
        writer.execute("DELETE FROM hub.agent")
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {len(load_migrations())}", 0)
