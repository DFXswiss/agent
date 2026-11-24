"""U-HUB-agents (own identity in the other areas, ADR-082 sections 1 and 9.5, 9.12): the confirmation wraps the user's
credential key to the agent; an own-identity agent writes and rotates its user's credential records as their author,
writes its user's GitHub identities and, for an admin, company accounts, sends device requests under its own ID
(follow-ups and responses compare through its user), and is a community author of its own while it is its user's
active agent - moderation stays its user's."""

from __future__ import annotations

import uuid

import pytest

from agent_hub.agents import store
from agent_hub.community import store as community_store
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.agents.own import OwnAgent, add_batch, own_agent, paired, submitted, user_uuid
from tests.platform.hub.agents.support import gesture_client, handover, rows
from tests.platform.hub.community.conftest import Community, Member
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.credentials.support import Desk
from tests.platform.hub.github.support import Studio
from tests.platform.hub.orgs.support import Actor, World


@pytest.fixture
def community(hub: HubHarness) -> Community:
    return Community(hub)


@pytest.fixture
def desk(hub: HubHarness, published: MemoryPublisher) -> Desk:
    built = Desk.build(World(hub))
    published.clear()
    return built


def admin_agent(desk: Desk) -> OwnAgent:
    """The scene admin's own-identity agent, with the admin's CK epoch 1 wrapped to it."""
    hub, admin, draft = desk.hub, desk.scene.admin, desk.scene.draft
    agent = paired(hub)
    recipient = Actor(str(agent.agent_id), "agent", agent.identity)
    batch = add_batch(draft, admin, agent)
    confirm = handover(
        agent, admin.id, admin.identity, batches=[batch], cred_wraps=[desk.wrap(admin, admin, 1, recipient)]
    )
    response = gesture_client(hub, user_uuid(admin), agent.agent_id).post("/v1/me/agents", json=confirm.body)
    assert response.status_code == 200, response.text
    submitted(draft)
    agent.user_id = user_uuid(admin)
    assert agent.client(hub).post("/v1/agent/claim", json=confirm.claim_body).status_code == 200
    return OwnAgent(agent, admin, Actor(recipient.id, "agent", agent.identity, agent.client(hub)), confirm)


def test_the_confirmation_wraps_the_users_credential_key_to_the_agent(desk: Desk) -> None:
    hub, admin, draft = desk.hub, desk.scene.admin, desk.scene.draft
    agent = paired(hub)
    recipient = Actor(str(agent.agent_id), "agent", agent.identity)
    session = gesture_client(hub, user_uuid(admin), agent.agent_id)
    # the current CK epoch is required once the agent is in a log of its user
    batch = add_batch(draft, admin, agent)
    draft.drop_pending()
    response = session.post("/v1/me/agents", json=handover(agent, admin.id, admin.identity, batches=[batch]).body)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "agent_batches_incomplete", "detail": {"expected": 1}},
    )
    # a wrap of another scope or for another recipient is no credential-key wrap of the confirmation
    for wrap, index in ((draft.wrap(admin, "org", draft.id, 1, recipient), 0), (desk.wrap(admin, admin, 1, admin), 0)):
        body = handover(agent, admin.id, admin.identity, batches=[batch], cred_wraps=[wrap]).body
        response = session.post("/v1/me/agents", json=body)
        assert response.json() == {"error": "request_invalid", "detail": {"pointers": [f"/cred_wraps/{index}"]}}
    own = admin_agent(desk)
    found = rows(
        hub,
        "SELECT epoch, wrapped_by FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s AND recipient_id = %s",
        admin.id,
        own.agent.agent_id,
    )
    assert found == [(1, uuid.UUID(admin.id))]
    listed = own.agent.client(hub).get("/v1/wraps", params={"scope": "cred"}).json()["wraps"]
    assert [(item["scope_id"], item["epoch"]) for item in listed] == [(admin.id, 1)]


def test_the_agent_writes_and_rotates_its_users_records_as_their_author(desk: Desk, published: MemoryPublisher) -> None:
    hub, admin = desk.hub, desk.scene.admin
    own = admin_agent(desk)
    agent = own.actor
    record = desk.record(admin, "github", author=agent)
    response = agent.browser.put("/v1/me/credentials/github", json={"record": record, "expected_version": 0})
    assert (response.status_code, response.json()) == (200, {"version": 1})
    assert rows(hub, "SELECT actor_kind, actor_id, agent_id FROM hub.audit WHERE action = 'credential.put'") == [
        ("user", uuid.UUID(admin.id), own.agent.agent_id)
    ]
    # a record signed by the agent but naming its user as author is refused
    forged = desk.record(admin, "mailbox", author=agent, header={"author": admin.id})
    response = agent.browser.put("/v1/me/credentials/mailbox", json={"record": forged, "expected_version": 0})
    assert (response.status_code, response.json()) == (400, {"error": "header_invalid"})
    # the rotation by the agent: records and wraps by the agent, the user's self-wrap included
    rotated = desk.record(admin, "github", version=2, epoch=2, author=agent, record_id=record["header"]["id"])
    recipients = [admin, agent, desk.scene.device, desk.scene.other_device]
    wraps = [desk.wrap(agent, admin, 2, recipient) for recipient in recipients]
    published.clear()
    response = agent.browser.post("/v1/me/credentials/rotate", json={"epoch": 2, "wraps": wraps, "records": [rotated]})
    assert (response.status_code, response.json()) == (200, {})
    assert rows(hub, "SELECT DISTINCT wrapped_by FROM hub.key_wrap WHERE scope = 'cred' AND epoch = 2") == [
        (own.agent.agent_id,)
    ]
    audiences = sorted((item.audience for item in published.items() if item.frame == "wraps"), key=str)
    assert audiences == sorted(
        [
            Audience("user", uuid.UUID(admin.id)),
            Audience("user", uuid.UUID(admin.id)),
            Audience("device", uuid.UUID(desk.scene.device.id)),
            Audience("device", uuid.UUID(desk.scene.other_device.id)),
        ],
        key=str,
    )
    assert rows(hub, "SELECT actor_id, agent_id FROM hub.audit WHERE action = 'credential.rotate'") == [
        (uuid.UUID(admin.id), own.agent.agent_id)
    ]
    # an agent whose certificate does not verify is no author
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.agent SET cert = %s WHERE id = %s", (bytes(64), own.agent.agent_id))
    third = desk.record(admin, "github", version=3, epoch=2, author=agent, record_id=record["header"]["id"])
    response = agent.browser.put("/v1/me/credentials/github", json={"record": third, "expected_version": 2})
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})


def test_the_agent_writes_its_users_github_identities_and_company_accounts(desk: Desk) -> None:
    studio, admin = Studio(desk), desk.scene.admin
    agent = admin_agent(desk).actor
    record = studio.personal(admin, author=agent)
    response = studio.put(agent, record, 0)
    assert (response.status_code, response.json()) == (200, {"version": 1})
    assert studio.primary_of(admin) == [record["header"]["id"]]
    # the admin's agent writes a company record as the admin does, its wraps for the org's devices included (KM-35)
    response = studio.put_company(studio.company(author=agent), 0, caller=agent, key=studio.key(1, wrapper=agent))
    assert (response.status_code, response.json()) == (200, {"version": 1})
    assert studio.rows(
        "SELECT DISTINCT wrapped_by FROM hub.key_wrap WHERE scope = 'cred' AND scope_id = %s", studio.org
    ) == [(uuid.UUID(agent.id),)]


def test_the_agent_sends_device_requests_under_its_own_id(desk: Desk, published: MemoryPublisher) -> None:
    admin, device = desk.scene.admin, desk.scene.device
    own = admin_agent(desk)
    agent = own.actor
    root = desk.request(agent, device, "claude.login")
    response = desk.send(agent, root)
    assert response.status_code == 200, response.text
    request_id = root["header"]["id"]
    assert desk.world.rows("SELECT user_id FROM hub.device_request WHERE id = %s", request_id) == [
        (uuid.UUID(admin.id),)
    ]
    # an envelope that names its user but is signed by the agent, or names the agent from the user, is refused
    assert desk.send(agent, desk.request(admin, device, signer=agent)).json() == {"error": "forbidden"}
    assert desk.send(admin, desk.request(agent, device, signer=admin)).json() == {"error": "forbidden"}
    # the user's follow-up of the agent's request: requesters compare through the user (ADR-082 section 9.12)
    follow = desk.request(admin, device, "claude.login", follows=request_id)
    assert desk.send(admin, follow).status_code == 200
    # the device answers the agent (its key from the log) or the user; anybody else is refused
    client = desk.client(device)
    assert client.get(f"/v1/device/requests/{request_id}").status_code == 200
    published.clear()
    answer = desk.response(device, agent, request_id, "claude.login", seq=1)
    assert client.post(f"/v1/device/requests/{request_id}/responses", json={"envelope": answer}).status_code == 200
    assert [item.audience for item in published.items() if item.frame == "device_response"] == [
        Audience("user", uuid.UUID(admin.id))
    ]
    stranger = desk.response(device, desk.scene.reader, request_id, "claude.login", seq=2)
    refused = client.post(f"/v1/device/requests/{request_id}/responses", json={"envelope": stranger})
    assert refused.json() == {"error": "header_invalid"}


# --- community --------------------------------------------------------------------------------------------------------


def community_agent(community: Community, person: Member) -> tuple[OwnAgent, Member]:
    """An own-identity agent of `person` as a community member of its own."""
    own = own_agent(community.hub, person.actor, [])
    return own, Member(community.hub, own.actor)


def test_the_agent_is_a_community_author_of_its_own(community: Community, published: MemoryPublisher) -> None:
    hub = community.hub
    founder = community.founder()
    person = community.member()
    own, agent = community_agent(community, person)
    agent.join()
    assert agent.request().status_code == 200
    served = founder.serve()
    assert served.status_code == 200, served.text
    # the key frame of the agent's request reaches its user's channel
    assert Audience("user", uuid.UUID(person.id)) in [item.audience for item in published.items()]
    agent.open_request()
    assert agent.post(agent.message("Ich bin der Agent")).status_code == 200
    page = founder.get("/v1/community/messages").json()
    assert page["messages"][0]["author"] == own.actor.id
    described = page["authors"][own.actor.id]
    assert described["sig_pub"] == own.agent.identity.public()["sig"]
    assert described["agent"] == {
        "user": person.id,
        "user_sig_pub": person.actor.identity.public()["sig"],
        "kem_pub": own.agent.identity.public()["kem"],
        "gen": 1,
        "cert": own.confirm.body["cert"],
    }
    audit = rows(hub, "SELECT actor_id, agent_id FROM hub.audit WHERE action = 'community.post'")
    assert audit == [(uuid.UUID(person.id), own.agent.agent_id)]
    # the agent of a moderator moderates with its user's right; its own messages it deletes as their author
    community.moderator(person)
    founder_post = founder.post()
    assert founder_post.status_code == 200
    message_id = founder.sent[-1]["header"]["id"]
    deleted = agent.client.delete(f"/v1/community/messages/{message_id}")
    assert deleted.status_code == 200, deleted.text
    assert community.world.rows("SELECT deleted_as FROM hub.community_message WHERE id = %s", message_id) == [
        ("moderator",)
    ]
    # a revoked agent's messages stay readable with its keys; it is no community principal any more
    session = person.actor.browser
    assert session.delete(f"/v1/me/agents/{own.agent.agent_id}").status_code == 200
    with hub.pool.connection() as connection:
        assert community_store.identity(connection, uuid.UUID(own.actor.id)) is None
    assert own.actor.id in founder.get("/v1/community/messages").json()["authors"]


def test_only_an_active_agent_with_a_valid_certificate_has_community_keys(community: Community) -> None:
    hub = community.hub
    person = community.member()
    own, _agent = community_agent(community, person)
    agent_id = own.agent.agent_id
    with hub.pool.connection() as connection:
        assert community_store.identity(connection, agent_id) == (
            own.agent.identity.kem_public(),
            own.agent.identity.sig_public(),
        )
        connection.execute("UPDATE hub.agent SET cert = %s WHERE id = %s", (bytes(64), agent_id))
        assert store.agent_keys(connection, agent_id) is None
        assert store.agent_authors(connection, [agent_id])[str(agent_id)]["agent"]["gen"] == 1
        assert store.acting_user(connection, agent_id) == uuid.UUID(person.id)
        assert store.acting_user(connection, uuid.UUID(person.id)) == uuid.UUID(person.id)
