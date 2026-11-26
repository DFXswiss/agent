"""U-HUB-agents (the main orchestrator on platform hardware, ADR-082 sections 6 and 9.6-9.9; 11-agent-access.md
section 11): only the browser session of the task owner asks for a running main session of the owner's private
organization on a platform device; the session's device registers the keys with its signature; the owner confirms as
every pairing and stores the signed handover, which the device fetches; the end of the session revokes the
orchestrator in the transition's transaction; one main orchestrator over both forms. Attacks: an agent, a device or
another user cannot ask, confirm or hand over; a session outside the private organization or on an org device is
refused."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from agent_hub.agents import orchestrator, store
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.keys import Identity
from agent_hub.errors import HubError
from agent_hub.orgs.routes import publish_append
from agent_hub.sessions.transitions import apply
from agent_hub.stream.publish import CLOSE_KEYS, MemoryPublisher
from tests.platform.hub.agents.own import agent_actor, confirm_body, own_agent, paired, submitted
from tests.platform.hub.agents.support import (
    TestAgent,
    agent_client,
    agent_row,
    certificate,
    gesture_client,
    handover,
    new_agent,
    rows,
)
from tests.platform.hub.conftest import HubHarness, SignedClient, contracts_registry
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import Actor, OrgDraft, World, new_id

NO_CONTRACT = {"contract": False}
ROOT_KEY = "A" * 43


@pytest.fixture
def world(hub: HubHarness) -> World:
    return World(hub)


@pytest.fixture
def fleet(world: World) -> Fleet:
    return Fleet(world)


@dataclass
class Scene:
    """The owner with a private organization and a main session on a platform device."""

    world: World
    fleet: Fleet
    user: Actor
    draft: OrgDraft
    device: Actor
    task: str
    session: str

    @property
    def hub(self) -> HubHarness:
        return self.world.hub

    @property
    def user_id(self) -> uuid.UUID:
        return uuid.UUID(self.user.id)

    @property
    def path(self) -> str:
        return f"/v1/sessions/{self.session}/orchestrator"

    def device_client(self, device: Actor | None = None) -> SignedClient:
        return self.fleet.client(device or self.device)

    def request(self, client: SignedClient | None = None, **options: Any) -> Any:
        return (client or self.user.browser).post(self.path, json={}, **options)

    def view(self) -> dict[str, Any]:
        response = self.user.browser.get(self.path)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def device_view(self) -> dict[str, Any]:
        response = self.device_client().get(f"/v1/device/sessions/{self.session}/orchestrator")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def set_state(self, state: str) -> None:
        execute(self.hub, "UPDATE hub.session SET state = %s WHERE id = %s", state, self.session)


def execute(hub: HubHarness, query: str, *params: object) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(query, params)


def insert_session(
    hub: HubHarness,
    draft: OrgDraft,
    owner: Actor,
    device: Actor | None,
    *,
    state: str = "working",
    parent: str | None = None,
    task: str | None = None,
) -> tuple[str, str]:
    """A task (unless given) and a session of it in `state` on `device`, as the scheduler and the outbox store them."""
    now = hub.clock()
    task_id, session_id = task or new_id(), new_id()
    if task is None:
        execute(
            hub,
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
            " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
            task_id,
            draft.id,
            draft.default_wg,
            owner.id,
            now,
            now,
            now,
        )
    execute(
        hub,
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        session_id,
        task_id,
        draft.id,
        draft.default_wg,
        owner.id,
        None if device is None else device.id,
        parent,
        0 if parent is None else 1,
        state,
        now,
    )
    return task_id, session_id


def build(
    world: World,
    fleet: Fleet,
    *,
    private: bool = True,
    kind: str = "platform",
    state: str = "working",
    local: bool = False,
) -> Scene:
    """The scene; `local`: the user has a claimed local main orchestrator (in no log yet) before the organization."""
    user = world.user()
    if local:
        own_agent(world.hub, user, [])
    draft = world.private_org(user) if private else world.public_org(user)
    device = world.device(kind)
    task, session = insert_session(world.hub, draft, user, device, state=state)
    return Scene(world, fleet, user, draft, device, task, session)


@pytest.fixture
def scene(world: World, fleet: Fleet) -> Scene:
    return build(world, fleet)


def hints(published: MemoryPublisher, device: Actor) -> list[dict[str, Any]]:
    """The `session.orchestrator` hints to the device, checked against their contract."""
    found = []
    for item in published.items():
        if item.audience.kind == "device" and item.frame == orchestrator.HINT and str(item.audience.id) == device.id:
            frame = {"type": item.frame, **item.data}
            contracts_registry().validate("ws/device/session.orchestrator.json", frame)
            found.append(frame)
    return found


def registration(scene: Scene, agent: TestAgent, *, device: Identity | None = None, **changes: Any) -> dict[str, Any]:
    """The body the runner sends: the session's keys with the device's signature over their fingerprint."""
    pub = agent.identity.public()
    fp = agent_format.fingerprint(agent.identity.kem_public(), agent.identity.sig_public())
    text = agent_format.orchestrator_key_text(fp, str(agent.agent_id), scene.session)
    body = {
        "session_id": scene.session,
        "pairing_id": str(agent.agent_id),
        "pub": pub,
        "pop": agent_format.sign_pop(agent.identity, str(agent.agent_id)),
        "fp": fp,
        "device_sig": agent_format.sign_text((device or scene.device.identity).sig, text),
    }
    return {**body, **changes}


def register(scene: Scene, agent: TestAgent | None = None) -> TestAgent:
    """Request (owner) and registration (device) of a session pairing."""
    found = agent or new_agent(scene.hub)
    assert scene.request().status_code == 200
    response = scene.device_client().post("/v1/device/orchestrator-pairings", json=registration(scene, found))
    assert response.status_code == 200, response.text
    return found


def confirm(scene: Scene, agent: TestAgent, *, gen: int = 1) -> Any:
    body = confirm_body(scene.hub, agent, scene.user, [scene.draft], gen=gen)
    response = gesture_client(scene.hub, scene.user_id, agent.agent_id).post("/v1/me/agents", json=body.body)
    assert response.status_code == 200, response.text
    submitted(scene.draft)
    agent.user_id = scene.user_id
    return body


def handover_body(scene: Scene, agent: TestAgent, secret: bytes, signer: Identity | None = None) -> dict[str, str]:
    ct = agent_format.seal_handover(agent.identity.kem_public(), str(agent.agent_id), scene.user.id, secret, ROOT_KEY)
    text = agent_format.orchestrator_handover_text(ct, str(agent.agent_id), scene.session)
    return {"ct": ct, "sig": agent_format.sign_text((signer or scene.user.identity).sig, text)}


def hand_over(scene: Scene, agent: TestAgent, secret: bytes) -> dict[str, str]:
    body = handover_body(scene, agent, secret)
    response = scene.user.browser.post(f"/v1/agent-pairings/{agent.agent_id}/orchestrator-handover", json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"pairing_id": str(agent.agent_id), "session_id": scene.session}
    return body


def orchestrator_of(scene: Scene) -> TestAgent:
    """A claimed orchestrator of the scene's session."""
    agent = register(scene)
    confirmed = confirm(scene, agent)
    hand_over(scene, agent, confirmed.secret)
    claimed = agent.client(scene.hub).post("/v1/agent/claim", json=confirmed.claim_body)
    assert claimed.status_code == 200, claimed.text
    return agent


def refused(response: Any, status: int, code: str, reason: str | None = None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == code
    if reason is not None:
        assert body["detail"] == {"reason": reason}


def audits(scene: Scene, action: str) -> list[tuple[Any, ...]]:
    return rows(
        scene.hub,
        "SELECT actor_kind, actor_id, target_id, session_id, device_id, detail FROM hub.audit WHERE action = %s"
        " ORDER BY seq",
        action,
    )


# --- the request ------------------------------------------------------------------------------------------------------


def test_the_owner_asks_and_the_device_is_hinted(scene: Scene, published: MemoryPublisher) -> None:
    view = scene.view()
    assert view == {
        "session_id": scene.session,
        "device_id": scene.device.id,
        "available": True,
        "reason": None,
        "requested_at": None,
        "pairing": None,
    }
    response = scene.request()
    assert response.status_code == 200, response.text
    now = scene.hub.clock()
    assert response.json() == {**view, "requested_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    assert hints(published, scene.device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    (row,) = audits(scene, "session.orchestrator")
    assert row == ("user", scene.user_id, scene.session, uuid.UUID(scene.session), uuid.UUID(scene.device.id), {})
    assert scene.device_view() == {
        "session_id": scene.session,
        "user_id": scene.user.id,
        "requested_at": response.json()["requested_at"],
        "requested": True,
        "pairing": None,
    }
    # asked again before the runner answered: the time moves, the device is hinted again
    scene.hub.clock.advance(5)
    assert scene.request().status_code == 200
    assert len(hints(published, scene.device)) == 2


def test_only_the_browser_session_of_the_task_owner_may_ask(scene: Scene, world: World) -> None:
    # the user's own agent (also an agent of an account without a passkey) is no gesture of the person
    agent = agent_client(scene.hub, scene.user_id)
    refused(scene.request(agent), 403, "forbidden")
    refused(agent.get(scene.path), 403, "forbidden")
    # a device - also the session's own - cannot ask
    refused(scene.request(scene.device_client()), 403, "forbidden")
    refused(scene.device_client().get(scene.path), 403, "forbidden")
    # a user who cannot read the session does not learn that it exists
    stranger = world.user()
    refused(scene.request(stranger.browser), 404, "not_found")
    refused(stranger.browser.get(scene.path), 404, "not_found")
    # a reader who is not the task owner may not
    reader = world.user()
    execute(
        scene.hub,
        "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq) VALUES (%s, %s, 'write', 1)",
        scene.draft.default_wg,
        reader.id,
    )
    execute(
        scene.hub,
        "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq)"
        " VALUES (%s, %s, 'user', 'member', 1)",
        scene.draft.id,
        reader.id,
    )
    refused(scene.request(reader.browser), 403, "forbidden")
    refused(reader.browser.get(scene.path), 403, "forbidden")
    # unknown or malformed sessions
    refused(scene.user.browser.post(f"/v1/sessions/{new_id()}/orchestrator", json={}), 404, "not_found")
    refused(scene.user.browser.get("/v1/sessions/x/orchestrator", extensions=NO_CONTRACT), 404, "not_found")
    assert scene.view()["requested_at"] is None


def test_a_session_outside_the_rule_is_refused(world: World, fleet: Fleet) -> None:
    cases: list[tuple[Scene, str]] = []
    cases.append((build(world, fleet, private=False), "not_private"))
    cases.append((build(world, fleet, kind="org"), "not_platform"))
    for state in ("requested", "prompt_delivering", "suspended", "resuming", "stopping", "stopped", "failed"):
        cases.append((build(world, fleet, state=state), "not_running"))
    shared = build(world, fleet)
    other = world.user()
    world.join(shared.draft, other)
    cases.append((shared, "not_private"))
    foreign = build(world, fleet)
    execute(world.hub, "UPDATE hub.org SET created_by = %s WHERE id = %s", new_id(), foreign.draft.id)
    cases.append((foreign, "not_private"))
    gone = build(world, fleet)
    execute(world.hub, "UPDATE hub.device SET state = 'decertified' WHERE id = %s", gone.device.id)
    cases.append((gone, "not_platform"))
    unplaced = build(world, fleet)
    execute(world.hub, "UPDATE hub.session SET device_id = NULL WHERE id = %s", unplaced.session)
    cases.append((unplaced, "not_platform"))
    main = build(world, fleet)
    _, child = insert_session(world.hub, main.draft, main.user, main.device, parent=main.session, task=main.task)
    cases.append((Scene(world, fleet, main.user, main.draft, main.device, main.task, child), "not_main"))
    # ADR-095 section 2: a session of an analysis task never holds the user's full agent access
    analysis = build(world, fleet)
    execute(
        world.hub,
        'UPDATE hub.task SET analysis = \'{"scope": "wg"}\'::jsonb WHERE id = %s',
        analysis.task,
    )
    cases.append((analysis, "analysis"))
    for case, reason in cases:
        view = case.view()
        assert (view["available"], view["reason"]) == (False, reason), reason
        refused(case.request(), 409, "orchestrator_unavailable", reason)
    assert world.rows("SELECT count(*) FROM hub.session WHERE orchestrator_at IS NOT NULL") == [(0,)]


# --- the keys ---------------------------------------------------------------------------------------------------------


def test_the_device_registers_the_keys_of_the_session(scene: Scene) -> None:
    agent = register(scene)
    view = scene.view()
    pairing = view["pairing"]
    fp = agent_format.fingerprint(agent.identity.kem_public(), agent.identity.sig_public())
    assert pairing["pairing_id"] == str(agent.agent_id)
    assert (pairing["pub"], pairing["fp"], pairing["state"], pairing["handover"]) == (
        agent.identity.public(),
        fp,
        "open",
        False,
    )
    agent_format.verify_text(
        scene.device.identity.sig_public(),
        pairing["device_sig"],
        agent_format.orchestrator_key_text(fp, str(agent.agent_id), scene.session),
    )
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["user_id"], row["session_id"], str(row["device_id"])) == (
        "open",
        None,
        uuid.UUID(scene.session),
        scene.device.id,
    )
    assert row["expires_at"] == scene.hub.clock() + timedelta(seconds=store.PAIRING_SECONDS)
    # the anonymous pairing view of the browser's fingerprint check shows it like every pairing
    assert scene.hub.anon.get(f"/v1/agent-pairings/{agent.agent_id}").json()["state"] == "open"
    # the request is answered: the runner has nothing more to do until the handover
    device = scene.device_view()
    assert device["requested"] is False
    assert device["pairing"] == {
        "pairing_id": str(agent.agent_id),
        "state": "open",
        "expires_at": pairing["expires_at"],
        "handover": None,
    }
    (audit,) = audits(scene, "agent.pairing")
    assert audit[:5] == ("device", uuid.UUID(scene.device.id), str(agent.agent_id), uuid.UUID(scene.session), audit[4])
    # a repeat is unchanged; the same ID with other keys is a conflict
    again = scene.device_client().post("/v1/device/orchestrator-pairings", json=registration(scene, agent))
    assert again.status_code == 200
    assert again.json() == {"pairing_id": str(agent.agent_id), "expires_at": pairing["expires_at"]}
    other = new_agent(scene.hub)
    clash = registration(scene, other, pairing_id=str(agent.agent_id))
    clash["pop"] = agent_format.sign_pop(other.identity, str(agent.agent_id))
    clash["device_sig"] = agent_format.sign_text(
        scene.device.identity.sig,
        agent_format.orchestrator_key_text(clash["fp"], str(agent.agent_id), scene.session),
    )
    refused(scene.device_client().post("/v1/device/orchestrator-pairings", json=clash), 409, "id_conflict")
    # while the pairing is open the owner's request is answered unchanged and nobody is hinted
    assert scene.request().json()["pairing"]["pairing_id"] == str(agent.agent_id)


def test_new_keys_replace_an_open_pairing_of_the_session(scene: Scene) -> None:
    first = register(scene)
    scene.hub.clock.advance(store.PAIRING_SECONDS + 1)
    assert scene.view()["pairing"] is None
    assert scene.device_view()["requested"] is False  # the expired pairing is newer than the request
    second = register(scene)
    assert agent_row(scene.hub, first.agent_id) is None
    assert scene.view()["pairing"]["pairing_id"] == str(second.agent_id)


def test_the_registration_is_bound_to_the_session_its_device_and_the_request(scene: Scene, world: World) -> None:
    agent = new_agent(scene.hub)
    post = scene.device_client().post
    path = "/v1/device/orchestrator-pairings"
    # nobody asked yet
    refused(post(path, json=registration(scene, agent)), 409, "orchestrator_unavailable", "not_requested")
    assert scene.request().status_code == 200
    # another platform device cannot register keys for the session
    stranger = world.device("platform")
    refused(scene.fleet.client(stranger).post(path, json=registration(scene, agent)), 403, "forbidden")
    refused(post(path, json=registration(scene, agent, session_id=new_id())), 404, "not_found")
    # the proof of possession, the fingerprint and the device's signature
    refused(
        post(path, json=registration(scene, agent, pop=registration(scene, new_agent(scene.hub))["pop"])),
        401,
        "signature_invalid",
    )
    refused(
        post(path, json=registration(scene, agent, fp=agent_format.fingerprint(b"\x01" * 32, b"\x02" * 32))),
        400,
        "request_invalid",
    )
    refused(post(path, json=registration(scene, agent, device=stranger.identity)), 401, "signature_invalid")
    other_session = agent_format.sign_text(
        scene.device.identity.sig,
        agent_format.orchestrator_key_text(
            agent_format.fingerprint(agent.identity.kem_public(), agent.identity.sig_public()),
            str(agent.agent_id),
            new_id(),
        ),
    )
    refused(post(path, json=registration(scene, agent, device_sig=other_session)), 401, "signature_invalid")
    # a browser or an agent is no device
    refused(scene.user.browser.post(path, json=registration(scene, agent)), 403, "forbidden")
    refused(agent_client(scene.hub, scene.user_id).post(path, json=registration(scene, agent)), 403, "forbidden")
    # the keys of an agent that is not revoked
    taken = paired(scene.hub)
    copy = TestAgent(identity=taken.identity, agent_id=new_agent(scene.hub).agent_id, user_id=None)
    refused(post(path, json=registration(scene, copy)), 409, "id_conflict")
    # a session that stopped running
    scene.set_state("suspended")
    refused(post(path, json=registration(scene, agent)), 409, "orchestrator_unavailable", "not_running")
    assert agent_row(scene.hub, agent.agent_id) is None


def test_a_confirmed_pairing_holds_the_session_only_until_its_claim_deadline(scene: Scene) -> None:
    agent = register(scene)
    confirm(scene, agent)
    path = "/v1/device/orchestrator-pairings"
    scene.hub.clock.advance(1)
    execute(scene.hub, "UPDATE hub.session SET orchestrator_at = %s WHERE id = %s", scene.hub.clock(), scene.session)
    refused(scene.device_client().post(path, json=registration(scene, new_agent(scene.hub))), 409, "id_conflict")
    # past the claim deadline the session takes new keys after a new request; the used keys are refused
    scene.hub.clock.advance(store.ORCHESTRATOR_CLAIM_SECONDS)
    assert scene.view()["pairing"] is None
    assert scene.request().status_code == 200
    refused(scene.device_client().post(path, json=registration(scene, agent)), 409, "id_conflict")
    fresh = new_agent(scene.hub)
    response = scene.device_client().post(path, json=registration(scene, fresh))
    assert response.status_code == 200, response.text
    assert scene.view()["pairing"]["pairing_id"] == str(fresh.agent_id)


def test_the_same_keys_reopen_an_expired_pairing_only_after_a_new_request(scene: Scene) -> None:
    agent = register(scene)
    path = "/v1/device/orchestrator-pairings"
    scene.hub.clock.advance(store.PAIRING_SECONDS + 1)
    assert scene.view()["pairing"] is None
    # the runner retries without a new request of the owner: nothing to answer
    refused(
        scene.device_client().post(path, json=registration(scene, agent)),
        409,
        "orchestrator_unavailable",
        "not_requested",
    )
    assert scene.request().status_code == 200
    response = scene.device_client().post(path, json=registration(scene, agent))
    assert response.status_code == 200, response.text
    expires = scene.hub.clock() + timedelta(seconds=store.PAIRING_SECONDS)
    assert response.json() == {
        "pairing_id": str(agent.agent_id),
        "expires_at": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    assert scene.view()["pairing"]["state"] == "open"
    assert scene.device_view()["requested"] is False


def test_the_device_view_is_only_for_the_sessions_device(scene: Scene, world: World) -> None:
    path = f"/v1/device/sessions/{scene.session}/orchestrator"
    refused(scene.fleet.client(world.device("platform")).get(path), 403, "forbidden")
    refused(scene.device_client().get(f"/v1/device/sessions/{new_id()}/orchestrator"), 404, "not_found")
    refused(scene.device_client().get("/v1/device/sessions/x/orchestrator", extensions=NO_CONTRACT), 404, "not_found")
    refused(scene.user.browser.get(path), 403, "forbidden")
    refused(agent_client(scene.hub, scene.user_id).get(path), 403, "forbidden")


# --- confirmation, handover, claim ------------------------------------------------------------------------------------


def test_the_owner_confirms_hands_over_and_the_session_claims(
    world: World, fleet: Fleet, published: MemoryPublisher
) -> None:
    scene = build(world, fleet, local=True)
    (local,) = rows(scene.hub, "SELECT id FROM hub.agent WHERE user_id = %s", scene.user.id)
    agent = register(scene)
    published.clear()
    confirmed = confirm(scene, agent, gen=2)
    # the session's pairing replaced the local main orchestrator: one main orchestrator over both forms
    previous = agent_row(scene.hub, local[0])
    assert previous is not None
    assert (previous["state"], previous["revoked_reason"]) == ("revoked", "replaced")
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert row["state"] == "confirmed"
    assert row["expires_at"] == scene.hub.clock() + timedelta(seconds=store.ORCHESTRATOR_CLAIM_SECONDS)
    assert scene.view()["pairing"]["state"] == "confirmed"
    assert scene.device_view()["pairing"]["handover"] is None
    body = hand_over(scene, agent, confirmed.secret)
    assert hints(published, scene.device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    assert scene.view()["pairing"]["handover"] is True
    handed = scene.device_view()["pairing"]["handover"]
    assert handed == body
    # the runner's check: the owner's signature over the sealed handover
    agent_format.verify_text(
        scene.user.identity.sig_public(),
        handed["sig"],
        agent_format.orchestrator_handover_text(handed["ct"], str(agent.agent_id), scene.session),
    )
    secret, root = agent_format.open_handover(agent.identity.kem, str(agent.agent_id), scene.user.id, handed["ct"])
    assert (secret, root) == (confirmed.secret, ROOT_KEY)
    (audit,) = audits(scene, "agent.handover")
    assert audit[0:3] == ("user", scene.user_id, str(agent.agent_id))
    assert audit[5] == {"agent": str(agent.agent_id)}
    # a repeat is unchanged and hints nobody; other values are a conflict
    published.clear()
    path = f"/v1/agent-pairings/{agent.agent_id}/orchestrator-handover"
    assert scene.user.browser.post(path, json=body).status_code == 200
    assert hints(published, scene.device) == []
    refused(
        scene.user.browser.post(path, json=handover_body(scene, agent, secrets.token_bytes(32))), 409, "id_conflict"
    )
    claimed = agent.client(scene.hub).post("/v1/agent/claim", json=confirmed.claim_body)
    assert claimed.status_code == 200, claimed.text
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["handover_ct"], row["handover_sig"]) == ("active", None, None)
    assert scene.view()["pairing"]["state"] == "active"
    assert scene.device_view()["pairing"] == {
        "pairing_id": str(agent.agent_id),
        "state": "active",
        "expires_at": scene.view()["pairing"]["expires_at"],
        "handover": None,
    }
    listed = {item["agent_id"]: item for item in scene.user.browser.get("/v1/me/agents").json()["agents"]}
    assert (listed[str(agent.agent_id)]["state"], listed[str(agent.agent_id)]["session_id"]) == (
        "active",
        scene.session,
    )
    # asked again: the session is the orchestrator already, nothing to do
    published.clear()
    assert scene.request().json()["pairing"]["state"] == "active"
    assert hints(published, scene.device) == []


def test_a_new_local_pairing_replaces_the_session_orchestrator(scene: Scene, published: MemoryPublisher) -> None:
    agent = orchestrator_of(scene)
    published.clear()
    successor = paired(scene.hub)
    user, draft = scene.user, scene.draft
    # the successor replaces the session's agent in the log: everything it read is rotated (ADR-082 section 9.1)
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
    recipient = agent_actor(scene.hub, successor)
    wraps = [
        draft.wrap(user, scope, scope_id, epoch, reader)
        for scope, scope_id in (("org", draft.id), ("wg", draft.default_wg))
        for epoch, reader in ((2, user), (2, recipient), (1, recipient))
    ]
    batch = {"org": draft.id, "expected_seq": draft.submitted, "entries": draft.pending(), "wraps": wraps}
    body = {**handover(successor, user.id, user.identity, gen=2).body, "batches": [batch]}
    response = gesture_client(scene.hub, scene.user_id, successor.agent_id).post("/v1/me/agents", json=body)
    assert response.status_code == 200, response.text
    assert hints(published, scene.device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_reason"]) == ("revoked", "replaced")
    assert rows(scene.hub, "SELECT count(*) FROM hub.agent WHERE state IN ('confirmed', 'active')") == [(1,)]


def test_only_the_task_owner_confirms_while_the_session_runs(scene: Scene, world: World) -> None:
    agent = register(scene)
    body = confirm_body(scene.hub, agent, scene.user, [scene.draft])
    # another person's passkey gesture
    other = world.user()
    other_body = confirm_body(scene.hub, agent, other, [])
    refused(
        gesture_client(scene.hub, uuid.UUID(other.id), agent.agent_id).post("/v1/me/agents", json=other_body.body),
        403,
        "forbidden",
    )
    # the user's agent (an account without a passkey may confirm its successor, never a session)
    execute(scene.hub, "DELETE FROM hub.passkey WHERE user_id = %s", scene.user.id)
    refused(agent_client(scene.hub, scene.user_id).post("/v1/me/agents", json=body.body), 403, "forbidden")
    # the session stopped running in between
    scene.set_state("suspended")
    gesture = gesture_client(scene.hub, scene.user_id, agent.agent_id)
    refused(gesture.post("/v1/me/agents", json=body.body), 409, "orchestrator_unavailable", "not_running")
    assert agent_row(scene.hub, agent.agent_id)["state"] == "open"  # type: ignore[index]


def test_the_handover_only_from_the_owner_with_the_owners_signature(scene: Scene, world: World) -> None:
    agent = register(scene)
    path = f"/v1/agent-pairings/{agent.agent_id}/orchestrator-handover"
    secret = secrets.token_bytes(32)
    # not confirmed yet
    refused(scene.user.browser.post(path, json=handover_body(scene, agent, secret)), 404, "pairing_unknown")
    # the user's agent gets the browser's answer for a pairing that is not confirmed
    refused(
        agent_client(scene.hub, scene.user_id).post(path, json=handover_body(scene, agent, secret)),
        404,
        "pairing_unknown",
    )
    confirmed = confirm(scene, agent)
    body = handover_body(scene, agent, confirmed.secret)
    # and is no person once it is (the route's principal check, here in the store)
    with scene.hub.pool.connection() as connection, pytest.raises(HubError) as caught:
        orchestrator.store_handover(
            connection,
            scene.user_id,
            agent.agent_id,
            ct=b"\x00" * 64,
            sig=body["sig"],
            now=scene.hub.clock(),
            person=False,
        )
    assert caught.value.code == "forbidden"
    refused(scene.device_client().post(path, json=body), 403, "forbidden")
    refused(world.user().browser.post(path, json=body), 404, "pairing_unknown")
    refused(scene.user.browser.post("/v1/agent-pairings/x/orchestrator-handover", json=body), 404, "pairing_unknown")
    # a handover signed by another key (the device's, or not over this ciphertext) is refused
    refused(
        scene.user.browser.post(path, json=handover_body(scene, agent, confirmed.secret, scene.device.identity)),
        401,
        "signature_invalid",
    )
    refused(
        scene.user.browser.post(path, json={**body, "sig": handover_body(scene, agent, secret)["sig"]}),
        401,
        "signature_invalid",
    )
    refused(scene.user.browser.post(path, json={**body, "ct": "AAAA"}, extensions=NO_CONTRACT), 400, "request_invalid")
    # a user without a registered identity cannot sign
    execute(scene.hub, "DELETE FROM hub.user_identity WHERE user_id = %s", scene.user.id)
    refused(scene.user.browser.post(path, json=body), 401, "signature_invalid")
    # once the session stopped running
    scene.set_state("suspended")
    refused(scene.user.browser.post(path, json=body), 409, "orchestrator_unavailable", "not_running")


def test_the_handover_ends_with_the_claim_deadline(scene: Scene) -> None:
    agent = register(scene)
    confirmed = confirm(scene, agent)
    hand_over(scene, agent, confirmed.secret)
    scene.hub.clock.advance(store.ORCHESTRATOR_CLAIM_SECONDS)
    with scene.hub.pool.connection() as connection, connection.transaction():
        assert store.clear_expired_handovers(connection, scene.hub.clock()) == 1
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["handover_ct"], row["handover_sig"]) == (None, None)
    refused(
        scene.user.browser.post(
            f"/v1/agent-pairings/{agent.agent_id}/orchestrator-handover",
            json=handover_body(scene, agent, confirmed.secret),
        ),
        404,
        "pairing_unknown",
    )


# --- reconcile --------------------------------------------------------------------------------------------------------


def state_work(scene: Scene) -> list[dict[str, Any]]:
    response = scene.device_client().get("/v1/device/state")
    assert response.status_code == 200, response.text
    return list(response.json()["orchestrators"])


def test_the_device_state_lists_the_orchestrator_work(scene: Scene, published: MemoryPublisher) -> None:
    assert state_work(scene) == []
    assert scene.request().status_code == 200
    assert state_work(scene) == [{"session_id": scene.session, "step": "keys", "pairing_id": None}]
    agent = new_agent(scene.hub)
    scene.device_client().post("/v1/device/orchestrator-pairings", json=registration(scene, agent))
    assert state_work(scene) == []
    confirmed = confirm(scene, agent)
    assert state_work(scene) == []
    hand_over(scene, agent, confirmed.secret)
    assert state_work(scene) == [{"session_id": scene.session, "step": "handover", "pairing_id": str(agent.agent_id)}]
    scene.set_state("suspended")
    assert state_work(scene) == []
    scene.set_state("idle")
    agent.client(scene.hub).post("/v1/agent/claim", json=confirmed.claim_body)
    assert state_work(scene) == []
    # the user disconnects it while the session runs: the device is hinted and the guest ends it
    published.clear()
    assert scene.user.browser.delete(f"/v1/me/agents/{agent.agent_id}").status_code == 200
    assert hints(published, scene.device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    revoked = [{"session_id": scene.session, "step": "revoked", "pairing_id": str(agent.agent_id)}]
    assert state_work(scene) == revoked
    assert state_work(scene) == revoked  # repeated while the session runs; the runner ends it once
    # a new request asks for keys first
    scene.hub.clock.advance(1)
    assert scene.request().status_code == 200
    assert state_work(scene) == [{"session_id": scene.session, "step": "keys", "pairing_id": None}]


# --- the end of the session -------------------------------------------------------------------------------------------


def transition(scene: Scene, state: str, reason: str | None = None) -> None:
    now = scene.hub.clock()
    with scene.hub.pool.connection() as connection, connection.transaction():
        applied = apply(connection, scene.session, state, reason, now, received_at=now, reported_by=scene.device.id)
    applied.effects.flush()


@pytest.mark.parametrize(("state", "reason"), [("stopped", None), ("failed", "agent_exited"), ("failed", "unassigned")])
def test_the_end_of_the_session_revokes_its_orchestrator(
    scene: Scene, published: MemoryPublisher, state: str, reason: str | None
) -> None:
    agent = orchestrator_of(scene)
    published.clear()
    transition(scene, state, reason)
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_reason"]) == ("revoked", "session_ended")
    closing = [item for item in published.items() if item.frame == CLOSE_KEYS]
    assert [(item.audience.id, item.data) for item in closing] == [(scene.user_id, {"key_ids": [agent.key_id]})]
    # the transition and the revocation are one transaction: the audit rows follow each other
    actions = [action for (action,) in rows(scene.hub, "SELECT action FROM hub.audit ORDER BY seq DESC LIMIT 2")]
    assert set(actions) == {"agent.revoke", f"session.{state}"}
    refused(agent.client(scene.hub).get("/v1/agent/me"), 401, "key_revoked")
    listed = scene.user.browser.get("/v1/me/agents").json()["agents"][0]
    assert (listed["state"], listed["revoked_reason"], listed["session_id"]) == (
        "revoked",
        "session_ended",
        scene.session,
    )


def test_the_end_of_the_session_ends_unclaimed_pairings_too(scene: Scene, world: World, fleet: Fleet) -> None:
    agent = register(scene)
    confirmed = confirm(scene, agent)
    hand_over(scene, agent, confirmed.secret)
    transition(scene, "stopped")
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_reason"], row["handover_ct"], row["handover_sig"]) == (
        "revoked",
        "session_ended",
        None,
        None,
    )
    refused(agent.client(scene.hub).post("/v1/agent/claim", json=confirmed.claim_body), 401, "key_revoked")
    other = build(world, fleet)
    pairing = register(other)
    transition(other, "failed", "vm_start_failed")
    assert agent_row(other.hub, pairing.agent_id) is None
    # a session without any orchestrator ends as before
    plain = build(world, fleet)
    transition(plain, "stopped")
    assert rows(plain.hub, "SELECT count(*) FROM hub.agent WHERE session_id = %s", plain.session) == [(0,)]


def test_a_suspended_session_gives_up_its_orchestrator(scene: Scene, published: MemoryPublisher) -> None:
    """The VM of a suspended session holds no keys: the place is never held by a dead agent (ADR-082 section 9.9)."""
    agent = orchestrator_of(scene)
    transition(scene, "idle")
    assert agent_row(scene.hub, agent.agent_id)["state"] == "active"  # type: ignore[index]
    published.clear()
    transition(scene, "suspended")
    assert hints(published, scene.device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_reason"]) == ("revoked", "session_ended")
    assert [item.data for item in published.items() if item.frame == CLOSE_KEYS] == [{"key_ids": [agent.key_id]}]
    # resumed, the session may be asked again
    transition(scene, "resuming")
    transition(scene, "working")
    assert scene.view()["pairing"] is None
    assert scene.request().json()["available"] is True


def test_a_new_member_of_the_private_organization_ends_the_orchestrator(
    scene: Scene, world: World, published: MemoryPublisher
) -> None:
    agent = orchestrator_of(scene)
    other = world.user()
    found = scene.draft.invite(scene.draft.founder, kind="user", role="member")
    scene.draft.append(scene.draft.founder)
    assert agent_row(scene.hub, agent.agent_id)["state"] == "active"  # type: ignore[index]
    scene.draft.accept(other, found)
    result = scene.draft.append(other)
    assert result.revoked == [(scene.user_id, agent.agent_id)]
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_reason"]) == ("revoked", "session_ended")
    published.clear()
    publish_append(result)
    assert hints(published, scene.device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    assert [item.data for item in published.items() if item.frame == CLOSE_KEYS] == [{"key_ids": [agent.key_id]}]
    assert scene.view()["reason"] == "not_private"


def test_a_decertified_device_ends_the_orchestrator(world: World, fleet: Fleet, published: MemoryPublisher) -> None:
    user = world.user()
    draft = world.private_org(user)
    device, _ = fleet.platform_device()
    task, session = insert_session(world.hub, draft, user, device)
    scene = Scene(world, fleet, user, draft, device, task, session)
    agent = orchestrator_of(scene)
    bystander = build(world, fleet)
    other = orchestrator_of(bystander)
    published.clear()
    fleet.platform.post({"type": "device_decertify", "device": device.id})
    assert hints(published, device) == [{"type": "session.orchestrator", "session_id": scene.session}]
    row = agent_row(scene.hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_reason"]) == ("revoked", "session_ended")
    assert [item.data for item in published.items() if item.frame == CLOSE_KEYS] == [{"key_ids": [agent.key_id]}]
    assert agent_row(scene.hub, other.agent_id)["state"] == "active"  # type: ignore[index]


def test_keys_are_registered_only_for_an_unanswered_request(scene: Scene) -> None:
    """After the session's orchestrator was replaced, its device cannot register keys again without a new request
    of the person (ADR-082 section 9.9)."""
    agent = orchestrator_of(scene)
    path = "/v1/device/orchestrator-pairings"
    with scene.hub.pool.connection() as connection, connection.transaction():
        store.revoke(connection, scene.user_id, agent.agent_id, scene.hub.clock())
    scene.hub.clock.advance(1)
    refused(
        scene.device_client().post(path, json=registration(scene, new_agent(scene.hub))),
        409,
        "orchestrator_unavailable",
        "not_requested",
    )
    assert scene.request().status_code == 200
    assert scene.device_client().post(path, json=registration(scene, new_agent(scene.hub))).status_code == 200
