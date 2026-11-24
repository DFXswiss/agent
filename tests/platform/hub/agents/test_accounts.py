"""U-HUB-agents (an account of an agent alone, rule E-27, KOR-62, 11-agent-access.md section 5.5).

`POST /v1/agent-accounts`: an agent creates an account through a platform or an org invitation without a passkey and
signs its requests at once (an account without a passkey is a valid state: `GET /v1/me`, `GET /v1/me/agents`, the
private genesis, the accept of an org invitation); every refusal leaves nothing behind; an identical repeat changes
nothing; the orphan job releases an agent account that never used its invitation and keeps one that did.
`POST /v1/recovery/finish` with `agent`: the recovery code makes a new agent the main orchestrator of an account
without a passkey (the previous one revoked); refused for an account with a passkey before the ceremony is consumed.
`POST /v1/me/agents` by an agent: allowed for an account without a passkey, `403 forbidden` with a passkey.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_core.signing import b64url_decode
from agent_hub.agents import store
from agent_hub.auth import jobs, recovery
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto import invite as invite_codes
from agent_hub.crypto.bundle import RECOVERY_SLOT, open_bundle, seal_bundle
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.kdf import kek_from_recovery_code
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.recovery import generate_recovery_code, recovery_id, sign_proof
from agent_hub.crypto.wrap import sign_wrap, wrap_key
from agent_hub.invites import signals as invite_signals
from tests.platform.hub.agents.support import TestAgent, agent_row, new_agent, rows, stored_agent
from tests.platform.hub.agents.test_remote import create_remote, remote_handover
from tests.platform.hub.auth.authenticator import INVITES, FakeBrowser, new_invite, seed_invite
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.invites.support import accept, create_invitation
from tests.platform.hub.orgs.support import Actor, OrgDraft, World
from tests.platform.hub.platform.conftest import Platform

DAY = 24 * 3600
METHOD = agent_format.METHOD_AGENT_SIGNATURE
LABEL = {"host": "build-01", "name": "Codex"}
# y = 1: the neutral element, a small-order encoding `ed25519_public_key_valid` refuses
SMALL_ORDER = b"\x01" + bytes(31)
NO_CONTRACT = {"contract": False}


@pytest.fixture(autouse=True)
def fresh_invite_signals() -> Iterator[None]:
    invite_signals._context.clear()
    yield
    invite_signals.wait()
    invite_signals._context.clear()


@dataclass
class AgentAccount:
    """What the agent tool draws for an account of its own, and the body of `POST /v1/agent-accounts`."""

    invite: invite_codes.InviteSecrets
    user_id: str
    identity: Identity = field(repr=False)
    code: bytes = field(repr=False)
    agent: TestAgent
    body: dict[str, Any]

    @property
    def answer(self) -> dict[str, str]:
        return {"user_id": self.user_id, "agent_id": str(self.agent.agent_id)}

    def client(self, hub: HubHarness) -> Any:
        return self.agent.client(hub)

    def actor(self, hub: HubHarness) -> Actor:
        return Actor(self.user_id, "user", self.identity, self.client(hub))


def sealed_agent(agent: TestAgent, user_id: str, identity: Identity) -> dict[str, Any]:
    """The agent with the identity it sealed to itself under a secret of its own, the label and the claim hash."""
    secret = secrets.token_bytes(32)
    agent_id = str(agent.agent_id)
    return {
        "agent_id": agent_id,
        "pub": agent.identity.public(),
        "bundle_ct": agent_format.seal_identity(agent.identity.kem_public(), secret, agent_id, user_id, identity),
        "label_ct": agent_format.seal_label(identity.kem_public(), agent_id, user_id, LABEL),
        "claim_hash": b64u_encode(agent_format.claim_hash(agent_format.claim(secret, agent_id, user_id))),
    }


def account_text(agent: TestAgent, invite_id: str, user_id: str, identity: Identity) -> bytes:
    return agent_format.account_text(
        agent_id=str(agent.agent_id),
        agent_kem=agent.identity.kem_public(),
        agent_sig=agent.identity.sig_public(),
        invite_id=invite_id,
        method=METHOD,
        user_id=user_id,
        user_kem=identity.kem_public(),
        user_sig=identity.sig_public(),
    )


def agent_account(
    hub: HubHarness,
    invite: invite_codes.InviteSecrets | str,
    *,
    user_id: str | None = None,
    code: bytes | None = None,
    agent: TestAgent | None = None,
) -> AgentAccount:
    """Draw identity, recovery code, `CK_1` and the agent key, and sign the account text with all three keys."""
    found = INVITES[invite] if isinstance(invite, str) else invite
    user = user_id or str(uuid7(hub.clock()))
    identity = Identity.generate()
    code = code or generate_recovery_code()
    agent = agent or new_agent(hub)
    agent.user_id = uuid.UUID(user)
    wrap = wrap_key(
        identity.kem_public(), scope="cred", scope_id=user, epoch=1, recipient=user, key=secrets.token_bytes(32)
    )
    text = account_text(agent, found.invite_id, user, identity)
    body = {
        "invite_id": found.invite_id,
        "user_id": user,
        "identity": {"kem_pub": b64u_encode(identity.kem_public()), "sig_pub": b64u_encode(identity.sig_public())},
        "recovery": {
            "recovery_id": recovery_id(code),
            "bundle_ct": seal_bundle(kek_from_recovery_code(code), user, RECOVERY_SLOT, identity),
        },
        "cred_wrap": {
            "epoch": 1,
            "wrap": wrap,
            "sig": sign_wrap(identity.sig, scope="cred", scope_id=user, epoch=1, recipient=user, wrap=wrap),
        },
        "agent": sealed_agent(agent, user, identity),
        "proof": {
            "method": METHOD,
            "inv_sig": agent_format.sign_text(found.signing, text),
            "id_sig": agent_format.sign_text(identity.sig, text),
            "agent_sig": agent_format.sign_text(agent.identity.sig, text),
        },
    }
    return AgentAccount(found, user, identity, code, agent, body)


def create(hub: HubHarness, account: AgentAccount, **request: Any) -> Any:
    return hub.anon.post("/v1/agent-accounts", json=account.body, **request)


def register(hub: HubHarness, invite: invite_codes.InviteSecrets | str) -> AgentAccount:
    account = agent_account(hub, invite)
    response = create(hub, account)
    assert response.status_code == 200, response.text
    assert response.json() == account.answer
    return account


def registered_user(hub: HubHarness, invite_id: str) -> str | None:
    [(user,)] = rows(hub, "SELECT registered_user FROM hub.invite WHERE id = %s", invite_id)
    return None if user is None else str(user)


def count(hub: HubHarness, query: str, *params: object) -> int:
    [(found,)] = rows(hub, query, *params)
    return int(found)


def audit(hub: HubHarness, action: str) -> list[tuple[Any, ...]]:
    return rows(
        hub,
        "SELECT result, actor_kind, actor_id, target_kind, target_id, agent_id, detail FROM hub.audit"
        " WHERE action = %s ORDER BY seq",
        action,
    )


def nothing_stored(hub: HubHarness, account: AgentAccount) -> None:
    user = uuid.UUID(account.user_id)
    assert registered_user(hub, account.invite.invite_id) is None
    assert count(hub, "SELECT count(*) FROM hub.user_identity WHERE user_id = %s", user) == 0
    assert agent_row(hub, account.agent.agent_id) is None
    assert audit(hub, "agent.register") == []


# --- registration ---------------------------------------------------------------------------------------------------


def test_an_agent_registers_through_a_platform_invitation_and_founds_its_private_org(hub: HubHarness) -> None:
    plat = Platform(hub)
    invite_id = plat.invite_user()
    account = register(hub, invite_id)
    user = uuid.UUID(account.user_id)
    agent_id = account.agent.agent_id
    client = account.client(hub)
    me = client.get("/v1/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert (body["user_id"], body["identity"], body["passkeys"], body["orgs"]) == (
        account.user_id,
        account.body["identity"],
        [],
        [],
    )
    [listed] = client.get("/v1/me/agents").json()["agents"]
    assert (listed["agent_id"], listed["state"], listed["replaces"], listed["remote"]) == (
        str(agent_id),
        "active",
        None,
        False,
    )
    assert listed["label_ct"] == account.body["agent"]["label_ct"]
    row = agent_row(hub, agent_id)
    assert row is not None
    assert (row["user_id"], row["state"], row["confirmed_at"], row["claimed_at"]) == (
        user,
        "active",
        hub.clock(),
        hub.clock(),
    )
    assert bytes(row["bundle_ct"]) == b64url_decode(account.body["agent"]["bundle_ct"])
    assert count(hub, "SELECT count(*) FROM hub.passkey WHERE user_id = %s", user) == 0
    assert registered_user(hub, invite_id) == account.user_id
    assert audit(hub, "agent.register") == [
        (
            "ok",
            "user",
            user,
            "agent",
            str(agent_id),
            agent_id,
            {"agent": str(agent_id), "invite_id": invite_id, "method": METHOD},
        )
    ]
    assert audit(hub, "invite.register") == [
        ("ok", "user", user, "invite", invite_id, agent_id, {"invite_id": invite_id, "kind": "user"})
    ]
    # the agent founds its private org through the platform invitation, signed with its own key
    draft = OrgDraft(World(hub), account.actor(hub), private=True)
    created = draft.create(invite_id)
    assert created.status_code == 200, created.text
    orgs = client.get("/v1/me").json()["orgs"]
    assert [(item["org_id"], item["role"]) for item in orgs] == [(draft.id, "admin")]
    # the invitation is used: the orphan job keeps the account
    hub.clock.advance(2 * DAY)
    assert jobs.release_orphan_registrations(hub.app.state.hub) == 0
    assert client.get("/v1/me").status_code == 200


def test_an_agent_registers_through_an_org_invitation_and_accepts_it(hub: HubHarness) -> None:
    world = World(hub)
    draft = world.public_org()
    invitation = create_invitation(draft)
    assert invitation.response.status_code == 200, invitation.response.text
    account = register(hub, invitation.secrets)
    accepted = accept(draft, account.actor(hub), invitation)
    assert accepted.status_code == 200, accepted.text
    me = account.client(hub).get("/v1/me").json()
    assert [(item["org_id"], item["role"]) for item in me["orgs"]] == [(draft.id, "member")]
    assert me["passkeys"] == []
    hub.clock.advance(2 * DAY)
    assert jobs.release_orphan_registrations(hub.app.state.hub) == 0
    assert account.client(hub).get("/v1/me").status_code == 200


def test_the_orphan_job_releases_an_agent_account_that_never_used_its_invitation(hub: HubHarness) -> None:
    invite_id = seed_invite(hub)
    account = register(hub, invite_id)
    hub.clock.advance(DAY - 1)
    assert jobs.release_orphan_registrations(hub.app.state.hub) == 0
    hub.clock.advance(1)
    assert jobs.release_orphan_registrations(hub.app.state.hub) == 1
    user = uuid.UUID(account.user_id)
    assert agent_row(hub, account.agent.agent_id) is None
    for table, column in (("principal", "id"), ("user_identity", "user_id"), ("key_wrap", "recipient_id")):
        assert count(hub, f"SELECT count(*) FROM hub.{table} WHERE {column} = %s", user) == 0
    assert registered_user(hub, invite_id) is None
    response = account.client(hub).get("/v1/me")
    assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})
    # the invitation is open again, for an agent as for a person
    again = register(hub, invite_id)
    assert registered_user(hub, invite_id) == again.user_id


def test_an_identical_repeat_answers_alike_and_anything_else_is_refused(hub: HubHarness) -> None:
    invite_id = seed_invite(hub)
    account = register(hub, invite_id)
    hub.clock.advance(60)
    repeat = create(hub, account)
    assert (repeat.status_code, repeat.json()) == (200, account.answer)
    assert len(audit(hub, "agent.register")) == 1
    assert count(hub, "SELECT count(*) FROM hub.agent") == 1
    # another account for the bound invitation, or the same account with another agent: one code for everything
    for other in (agent_account(hub, invite_id), agent_account(hub, invite_id, user_id=account.user_id)):
        refused = create(hub, other)
        assert (refused.status_code, refused.json()) == (404, {"error": "invite_unknown"})
    # once the agent is revoked the repeat is no repeat any more
    with hub.pool.connection() as connection:
        store.revoke(connection, uuid.UUID(account.user_id), account.agent.agent_id, hub.clock())
    refused = create(hub, account)
    assert (refused.status_code, refused.json()) == (404, {"error": "invite_unknown"})
    assert registered_user(hub, invite_id) == account.user_id


@pytest.mark.parametrize("state", ["unknown", "bound", "used", "revoked", "expired", "device"])
def test_an_invitation_that_is_not_open_is_unknown(hub: HubHarness, state: str) -> None:
    found = new_invite()
    if state != "unknown":
        choices: dict[str, dict[str, Any]] = {
            "bound": {"registered_user": hub.keys.session().principal.principal_id},
            "used": {"used": True},
            "revoked": {"revoked": True},
            "expired": {"expires_in": -1},
            "device": {"kind": "device"},
        }
        options = choices[state]
        seed_invite(hub, found.invite_id, **options)
        with hub.pool.connection() as connection:
            connection.execute(
                "UPDATE hub.invite SET pk_inv = %s WHERE id = %s", (b64url_decode(found.pk_inv), found.invite_id)
            )
    account = agent_account(hub, found)
    response = create(hub, account)
    assert (response.status_code, response.json()) == (404, {"error": "invite_unknown"})
    assert count(hub, "SELECT count(*) FROM hub.user_identity") == 0
    assert agent_row(hub, account.agent.agent_id) is None


@pytest.mark.parametrize("signature", ["inv_sig", "id_sig", "agent_sig"])
def test_each_of_the_three_signatures_is_checked(hub: HubHarness, signature: str) -> None:
    invite_id = seed_invite(hub)
    account = agent_account(hub, invite_id)
    text = account_text(account.agent, invite_id, account.user_id, account.identity)
    other = {
        "inv_sig": new_invite().signing,
        "id_sig": Identity.generate().sig,
        "agent_sig": agent_format.new_agent().sig,
    }[signature]
    account.body["proof"][signature] = agent_format.sign_text(other, text)
    response = create(hub, account)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    nothing_stored(hub, account)


def test_the_proof_binds_the_invitation_the_user_and_the_keys(hub: HubHarness) -> None:
    invite_id = seed_invite(hub)
    for place in ("agent", "identity"):
        account = agent_account(hub, invite_id)
        # valid signatures of the right keys, but over a text with another KEM key
        if place == "agent":
            account.body["agent"]["pub"]["kem"] = b64u_encode(secrets.token_bytes(32))
        else:
            account.body["identity"]["kem_pub"] = b64u_encode(secrets.token_bytes(32))
        response = create(hub, account)
        assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
        nothing_stored(hub, account)
    # signatures over the text of another invitation
    account = agent_account(hub, seed_invite(hub))
    account.body["invite_id"] = invite_id
    response = create(hub, account)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})


@pytest.mark.parametrize(
    ("place", "pointer"),
    [("identity", "/identity/sig_pub"), ("agent", "/agent/pub/sig")],
)
def test_a_signature_key_that_is_no_valid_point_is_refused(hub: HubHarness, place: str, pointer: str) -> None:
    account = agent_account(hub, seed_invite(hub))
    if place == "identity":
        account.body["identity"]["sig_pub"] = b64u_encode(SMALL_ORDER)
    else:
        account.body["agent"]["pub"]["sig"] = b64u_encode(SMALL_ORDER)
    response = create(hub, account)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "params_invalid", "detail": {"pointers": [pointer]}},
    )
    nothing_stored(hub, account)


def test_a_self_wrap_not_signed_by_the_identity_is_refused(hub: HubHarness) -> None:
    account = agent_account(hub, seed_invite(hub))
    user = account.user_id
    wrap = account.body["cred_wrap"]["wrap"]
    account.body["cred_wrap"]["sig"] = sign_wrap(
        Identity.generate().sig, scope="cred", scope_id=user, epoch=1, recipient=user, wrap=wrap
    )
    response = create(hub, account)
    assert (response.status_code, response.json()) == (400, {"error": "wrap_unauthorized"})
    nothing_stored(hub, account)


@pytest.mark.parametrize("value", ["bundle_ct", "label_ct"])
def test_a_sealed_value_shorter_than_48_bytes_is_refused(hub: HubHarness, value: str) -> None:
    account = agent_account(hub, seed_invite(hub))
    account.body["agent"][value] = b64u_encode(secrets.token_bytes(40))
    response = create(hub, account)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": [f"/agent/{value}"]}},
    )
    nothing_stored(hub, account)


@pytest.mark.parametrize("taken", ["user", "recovery", "agent_id", "agent_key"])
def test_a_taken_id_or_key_is_a_conflict_and_stores_nothing(hub: HubHarness, taken: str) -> None:
    first = register(hub, seed_invite(hub))
    invite_id = seed_invite(hub)
    if taken == "user":
        account = agent_account(hub, invite_id, user_id=first.user_id)
    elif taken == "recovery":
        account = agent_account(hub, invite_id, code=first.code)
    elif taken == "agent_id":
        reused = new_agent(hub)
        reused.agent_id = first.agent.agent_id
        account = agent_account(hub, invite_id, agent=reused)
    else:
        other = stored_agent(hub)  # an active agent of another user: its key is taken
        account = agent_account(hub, invite_id, agent=TestAgent(other.identity, uuid7(hub.clock()), None))
    response = create(hub, account)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert registered_user(hub, invite_id) is None
    assert len(audit(hub, "agent.register")) == 1
    assert count(hub, "SELECT count(*) FROM hub.user_identity") == 1
    assert count(hub, "SELECT count(*) FROM hub.agent WHERE user_id = %s", uuid.UUID(account.user_id)) == (
        1 if taken == "user" else 0
    )


def test_a_malformed_body_is_refused(hub: HubHarness) -> None:
    account = agent_account(hub, seed_invite(hub))
    for change in ({"method": "webauthn"}, {"extra": True}):
        body = {**account.body, "proof": {**account.body["proof"], **change}}
        response = hub.anon.post("/v1/agent-accounts", json=body, extensions=NO_CONTRACT)
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid")
    nothing_stored(hub, account)


def test_a_passkey_registration_with_a_taken_user_id_is_a_conflict(hub: HubHarness) -> None:
    """`register.store_account` (shared since KOR-62) refuses a principal that exists already."""
    browser = FakeBrowser(hub)
    invite_id = seed_invite(hub)
    begun = browser.register_begin(invite_id).json()
    hub.keys.session(uuid.UUID(begun["user_id"]))  # the user ID is taken meanwhile
    credential = browser.authenticator.create(begun["options"])
    body, *_ = browser.registration_body(begun, credential)
    response = hub.anon.post("/v1/passkeys/register/finish", json=body)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert registered_user(hub, invite_id) is None
    assert count(hub, "SELECT count(*) FROM hub.user_identity") == 0


# --- recovery with an agent -----------------------------------------------------------------------------------------


@pytest.fixture
def generous(hub: HubHarness) -> None:
    hub.app.state.recovery_limit = recovery.SourceLimit(limit=1000)


def begin(hub: HubHarness, code: bytes) -> dict[str, Any]:
    response = hub.anon.post("/v1/recovery/begin", json={"recovery_id": recovery_id(code)})
    assert response.status_code == 200, response.text
    started: dict[str, Any] = response.json()
    return started


def recover_body(
    started: dict[str, Any],
    identity: Identity,
    agent: TestAgent,
    *,
    id_signer: Identity | None = None,
    agent_signer: Identity | None = None,
) -> dict[str, Any]:
    """The `recovery/finish` body of a new agent: the identity and the agent key sign the recover text."""
    user_id = started["user_id"]
    challenge = b64url_decode(started["challenge"]) or b""
    text = agent_format.recover_text(
        agent_id=str(agent.agent_id),
        agent_kem=agent.identity.kem_public(),
        agent_sig=agent.identity.sig_public(),
        challenge=challenge,
        method=METHOD,
        user_id=user_id,
    )
    return {
        "user_id": user_id,
        "challenge": started["challenge"],
        "proof": agent_format.sign_text((id_signer or identity).sig, text),
        "agent": {
            **sealed_agent(agent, user_id, identity),
            "method": METHOD,
            "agent_sig": agent_format.sign_text((agent_signer or agent.identity).sig, text),
        },
    }


def opened(started: dict[str, Any], code: bytes) -> Identity:
    return open_bundle(kek_from_recovery_code(code), started["user_id"], RECOVERY_SLOT, started["bundle_ct"])


def moved_agent(hub: HubHarness, user_id: str) -> TestAgent:
    agent = new_agent(hub)
    agent.user_id = uuid.UUID(user_id)
    return agent


def test_the_recovery_code_makes_a_new_agent_the_main_orchestrator(hub: HubHarness) -> None:
    account = register(hub, seed_invite(hub))
    user = uuid.UUID(account.user_id)
    hub.clock.advance(60)
    started = begin(hub, account.code)
    identity = opened(started, account.code)
    assert identity.public() == account.identity.public()
    agent = moved_agent(hub, account.user_id)
    finished = hub.anon.post("/v1/recovery/finish", json=recover_body(started, identity, agent))
    assert finished.status_code == 200, finished.text
    assert finished.json() == {"user_id": account.user_id, "agent_id": str(agent.agent_id)}
    # the previous agent is revoked, the new one signs at once
    old = account.client(hub).get("/v1/me")
    assert (old.status_code, old.json()) == (401, {"error": "key_revoked"})
    client = agent.client(hub)
    assert client.get("/v1/me").json()["identity"] == account.body["identity"]
    listed = client.get("/v1/me/agents").json()["agents"]
    assert [(item["agent_id"], item["state"], item["replaces"]) for item in listed] == [
        (str(agent.agent_id), "active", str(account.agent.agent_id)),
        (str(account.agent.agent_id), "revoked", None),
    ]
    assert [row[0] for row in rows(hub, "SELECT used_at FROM hub.ceremony WHERE kind = 'recovery'")] == [hub.clock()]
    assert [(row[0], row[-1]) for row in audit(hub, "auth.recovery")] == [
        ("ok", {"step": "begin"}),
        ("ok", {"step": "finish"}),
    ]
    assert audit(hub, "agent.register")[-1] == (
        "ok",
        "user",
        user,
        "agent",
        str(agent.agent_id),
        agent.agent_id,
        {"agent": str(agent.agent_id), "method": METHOD},
    )
    notices = rows(hub, "SELECT kind, org_id, ref FROM hub.notification WHERE recipient_id = %s", user)
    assert notices == [("agent.account_change", None, f"recovery_finished::{agent.agent_id}")]
    # the new agent replaces the recovery code with its signed request; the old code is gone
    code = generate_recovery_code()
    bundle = seal_bundle(kek_from_recovery_code(code), account.user_id, RECOVERY_SLOT, identity)
    put = client.put("/v1/me/bundles/recovery", json={"bundle_ct": bundle, "new_recovery_id": recovery_id(code)})
    assert put.status_code == 200, put.text
    gone = hub.anon.post("/v1/recovery/begin", json={"recovery_id": recovery_id(account.code)})
    assert gone.status_code == 404
    # the used challenge does not work again
    again = hub.anon.post(
        "/v1/recovery/finish", json=recover_body(started, identity, moved_agent(hub, account.user_id))
    )
    assert (again.status_code, again.json()) == (400, {"error": "ceremony_invalid"})


def test_an_account_with_a_passkey_cannot_recover_with_an_agent(hub: HubHarness) -> None:
    browser = FakeBrowser(hub)
    person = browser.register(seed_invite(hub))
    started = begin(hub, person.recovery_code)
    identity = opened(started, person.recovery_code)
    body = recover_body(started, identity, moved_agent(hub, person.user_id))
    response = hub.anon.post("/v1/recovery/finish", json=body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert agent_row(hub, uuid.UUID(body["agent"]["agent_id"])) is None
    assert audit(hub, "agent.register") == []
    # nothing was consumed: the same challenge still takes the person's proof
    challenge = b64url_decode(started["challenge"]) or b""
    proof = {
        "user_id": started["user_id"],
        "challenge": started["challenge"],
        "proof": sign_proof(identity.sig, started["user_id"], challenge),
    }
    finished = hub.anon.post("/v1/recovery/finish", json=proof)
    assert finished.status_code == 200, finished.text
    assert set(finished.json()) >= {"ceremony_id", "options"}


@pytest.mark.usefixtures("generous")
@pytest.mark.parametrize("wrong", ["proof", "agent_sig"])
def test_a_wrong_signature_burns_the_challenge_and_changes_nothing(hub: HubHarness, wrong: str) -> None:
    account = register(hub, seed_invite(hub))
    started = begin(hub, account.code)
    identity = opened(started, account.code)
    agent = moved_agent(hub, account.user_id)
    signers = {"id_signer": Identity.generate()} if wrong == "proof" else {"agent_signer": agent_format.new_agent()}
    response = hub.anon.post("/v1/recovery/finish", json=recover_body(started, identity, agent, **signers))
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    assert [(row[0], row[-1]) for row in audit(hub, "auth.recovery")][-1] == ("denied", {"step": "finish"})
    right = hub.anon.post("/v1/recovery/finish", json=recover_body(started, identity, agent))
    assert (right.status_code, right.json()) == (400, {"error": "ceremony_invalid"})
    assert agent_row(hub, agent.agent_id) is None
    assert account.client(hub).get("/v1/me").status_code == 200
    assert len(audit(hub, "agent.register")) == 1
    # a fresh challenge works
    started = begin(hub, account.code)
    finished = hub.anon.post("/v1/recovery/finish", json=recover_body(started, identity, agent))
    assert finished.status_code == 200, finished.text


def test_a_taken_agent_key_is_a_conflict_and_rolls_the_recovery_back(hub: HubHarness) -> None:
    account = register(hub, seed_invite(hub))
    other = stored_agent(hub)
    started = begin(hub, account.code)
    identity = opened(started, account.code)
    taken = TestAgent(other.identity, uuid7(hub.clock()), uuid.UUID(account.user_id))
    response = hub.anon.post("/v1/recovery/finish", json=recover_body(started, identity, taken))
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    # the previous agent stays active, nothing is audited or used, and the challenge is still the same
    assert account.client(hub).get("/v1/me").status_code == 200
    row = agent_row(hub, account.agent.agent_id)
    assert row is not None
    assert row["state"] == "active"
    assert agent_row(hub, taken.agent_id) is None
    assert [row[-1] for row in audit(hub, "auth.recovery")] == [{"step": "begin"}]
    assert rows(hub, "SELECT used_at FROM hub.ceremony WHERE kind = 'recovery'") == [(None,)]
    fresh = moved_agent(hub, account.user_id)
    finished = hub.anon.post("/v1/recovery/finish", json=recover_body(started, identity, fresh))
    assert finished.status_code == 200, finished.text


def test_a_recovery_agent_with_a_bad_key_or_short_bundle_is_refused(hub: HubHarness) -> None:
    account = register(hub, seed_invite(hub))
    started = begin(hub, account.code)
    identity = opened(started, account.code)
    body = recover_body(started, identity, moved_agent(hub, account.user_id))
    short = {**body, "agent": {**body["agent"], "label_ct": b64u_encode(secrets.token_bytes(40))}}
    response = hub.anon.post("/v1/recovery/finish", json=short)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/agent/label_ct"]}},
    )
    point = {**body, "agent": {**body["agent"], "pub": {**body["agent"]["pub"], "sig": b64u_encode(SMALL_ORDER)}}}
    response = hub.anon.post("/v1/recovery/finish", json=point)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "params_invalid", "detail": {"pointers": ["/agent/pub/sig"]}},
    )
    # neither attempt touched the ceremony
    finished = hub.anon.post("/v1/recovery/finish", json=body)
    assert finished.status_code == 200, finished.text


# --- confirmation by an agent ---------------------------------------------------------------------------------------


def test_the_agent_of_an_account_without_a_passkey_replaces_itself(hub: HubHarness) -> None:
    account = register(hub, seed_invite(hub))
    user = uuid.UUID(account.user_id)
    successor, _answer = create_remote(hub)
    # the old form (the identity sealed to the successor) stays open to an account of an agent alone (ADR-082 §7)
    confirm = remote_handover(successor, user, account.identity, old=True)
    response = account.client(hub).post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(successor.agent_id)})
    # the confirming agent is revoked at once and named in `replaces`
    old = account.client(hub).get("/v1/me")
    assert (old.status_code, old.json()) == (401, {"error": "key_revoked"})
    row = agent_row(hub, successor.agent_id)
    assert row is not None
    assert (row["state"], row["replaces"]) == ("confirmed", account.agent.agent_id)
    assert [detail for (*_rest, detail) in audit(hub, "agent.confirm")] == [
        {"agent": str(successor.agent_id), "remote": True}
    ]
    # the account notice names the acting agent and the new pairing (no person confirmed it)
    notices = rows(hub, "SELECT kind, org_id, ref FROM hub.notification WHERE recipient_id = %s", user)
    assert notices == [
        ("agent.account_change", None, f"agent_paired_remote:{account.agent.agent_id}:{successor.agent_id}")
    ]
    # an identical repeat by the (now revoked) agent is refused; nothing is notified twice
    assert account.client(hub).post("/v1/me/agents", json=confirm.body).status_code == 401
    # the successor fetches the handover and claims as after a person's confirmation
    successor.user_id = user
    client = successor.client(hub)
    fetched = client.get("/v1/agent/handover").json()
    secret, _root = agent_format.open_handover(
        successor.identity.kem, str(successor.agent_id), account.user_id, fetched["handover_ct"]
    )
    claim = agent_format.claim(secret, str(successor.agent_id), account.user_id)
    claimed = client.post("/v1/agent/claim", json={"claim": b64u_encode(claim)})
    assert claimed.status_code == 200, claimed.text
    assert client.get("/v1/me").json()["passkeys"] == []


def test_the_agent_of_an_account_with_a_passkey_cannot_confirm(hub: HubHarness) -> None:
    user = uuid7(hub.clock())
    hub.keys.session(user)  # the account has a passkey
    agent = stored_agent(hub, user)
    successor, _answer = create_remote(hub)
    confirm = remote_handover(successor, user, Identity.generate())
    response = agent.client(hub).post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    for found, state in ((successor, "open"), (agent, "active")):
        row = agent_row(hub, found.agent_id)
        assert row is not None
        assert row["state"] == state
    assert audit(hub, "agent.confirm") == []
