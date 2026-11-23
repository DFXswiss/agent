"""U-HUB-agents (pairing life cycle, 11-agent-access.md sections 2, 4 and 5, ADR-056 section 3): the agent registers
its keys with a proof of possession, the user's browser confirms with the session of the pairing-tagged login (never an
agent; the new agent replaces the user's main orchestrator), the agent claims with the secret from the loopback."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from agent_hub.agents import store
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_hub.crypto.keys import Identity
from agent_hub.errors import HubError
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.agents.support import (
    TestAgent,
    agent_row,
    gesture_client,
    handover,
    new_agent,
    rows,
    stored_agent,
)
from tests.platform.hub.auth.authenticator import FakeBrowser, Session, seed_invite
from tests.platform.hub.conftest import HubHarness, uuid7

NO_CONTRACT = {"contract": False}


def paired(hub: HubHarness) -> TestAgent:
    agent = new_agent(hub)
    response = hub.anon.post("/v1/agent-pairings", json=agent.pairing_body())
    assert response.status_code == 200, response.text
    return agent


def user_with_identity(hub: HubHarness) -> tuple[uuid.UUID, Identity]:
    user, identity = uuid7(hub.clock()), Identity.generate()
    hub.keys.session(user)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.user_identity (user_id, kem_pub, sig_pub, recovery_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user, identity.kem_public(), identity.sig_public(), b64u_encode(uuid.uuid4().bytes), hub.clock()),
        )
    return user, identity


# --- POST /v1/agent-pairings ------------------------------------------------------------------------------------------


def test_a_pairing_is_created_with_a_proof_of_possession_and_repeated_idempotently(hub: HubHarness) -> None:
    agent = new_agent(hub)
    response = hub.anon.post("/v1/agent-pairings", json=agent.pairing_body())
    expires = format_time(hub.clock() + timedelta(seconds=600))
    assert (response.status_code, response.json()) == (200, {"pairing_id": str(agent.agent_id), "expires_at": expires})
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["user_id"], row["source"], row["claim_misses"]) == ("open", None, "testclient", 0)
    assert (bytes(row["kem_pub"]), bytes(row["sig_pub"])) == (agent.identity.kem_public(), agent.identity.sig_public())
    hub.clock.advance(5)
    again = hub.anon.post("/v1/agent-pairings", json=agent.pairing_body())
    assert (again.status_code, again.json()) == (200, response.json())


def test_a_proof_for_another_pairing_or_key_is_refused(hub: HubHarness) -> None:
    agent, other = new_agent(hub), new_agent(hub)
    body = agent.pairing_body()
    replayed = {**body, "pairing_id": str(other.agent_id)}
    response = hub.anon.post("/v1/agent-pairings", json=replayed)
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    foreign_kem = {**body, "pub": {**body["pub"], "kem": other.identity.public()["kem"]}}
    assert hub.anon.post("/v1/agent-pairings", json=foreign_kem).json() == {"error": "signature_invalid"}
    # 32 bytes that are no Ed25519 point
    broken = {**body, "pub": {**body["pub"], "sig": b64u_encode(b"\xff" * 32)}}
    assert hub.anon.post("/v1/agent-pairings", json=broken).json() == {"error": "signature_invalid"}
    assert rows(hub, "SELECT count(*) FROM hub.agent") == [(0,)]


def test_other_keys_under_a_used_id_and_used_keys_under_a_new_id_are_conflicts(hub: HubHarness) -> None:
    agent = paired(hub)
    other = new_agent(hub)
    swapped = TestAgent(identity=other.identity, agent_id=agent.agent_id, user_id=None)
    response = hub.anon.post("/v1/agent-pairings", json=swapped.pairing_body())
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    reused = TestAgent(identity=agent.identity, agent_id=other.agent_id, user_id=None)
    response = hub.anon.post("/v1/agent-pairings", json=reused.pairing_body())
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    # the keys of a revoked agent may be paired again
    revoked = stored_agent(hub, state="revoked")
    again = TestAgent(identity=revoked.identity, agent_id=uuid7(hub.clock()), user_id=None)
    assert hub.anon.post("/v1/agent-pairings", json=again.pairing_body()).status_code == 200


def test_malformed_pairings_are_request_invalid(hub: HubHarness) -> None:
    agent = new_agent(hub)
    body = agent.pairing_body()
    for bad in ({**body, "pairing_id": str(uuid.uuid4())}, {**body, "pop": "x"}, {**body, "extra": 1}):
        response = hub.anon.post("/v1/agent-pairings", json=bad, extensions=NO_CONTRACT)
        assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_three_open_pairings_per_source_then_rate_limited(hub: HubHarness) -> None:
    for _ in range(3):
        paired(hub)
    response = hub.anon.post("/v1/agent-pairings", json=new_agent(hub).pairing_body())
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert response.headers["retry-after"] == "60"
    # expired pairings do not count, nor confirmed ones
    hub.clock.advance(601)
    assert hub.anon.post("/v1/agent-pairings", json=new_agent(hub).pairing_body()).status_code == 200


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("198.51.100.7", "198.51.100.7"),
        ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
        ("::ffff:198.51.100.7", "198.51.100.7"),
        ("testclient", "testclient"),
        ("x" * 80, "x" * 64),
    ],
)
def test_the_source_key(address: str, key: str) -> None:
    assert store.source_key(address) == key


def create(hub: HubHarness, source: str) -> None:
    agent = new_agent(hub)
    body = agent.pairing_body()
    with hub.pool.connection() as connection, connection.transaction():
        store.create_pairing(
            connection,
            agent.agent_id,
            kem=body["pub"]["kem"],
            sig=body["pub"]["sig"],
            pop=body["pop"],
            source=source,
            now=hub.clock(),
        )


def test_the_per_source_limit_counts_an_ipv6_network_and_mapped_addresses_together(hub: HubHarness) -> None:
    for address in ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:2:ffff::3"):
        create(hub, address)
    with pytest.raises(HubError) as refused:
        create(hub, "2001:db8:1:2::4")
    assert refused.value.code == "rate_limited"
    create(hub, "2001:db8:1:3::1")  # another /64
    for address in ("198.51.100.7", "::ffff:198.51.100.7", "198.51.100.7"):
        create(hub, address)
    with pytest.raises(HubError):
        create(hub, "::ffff:198.51.100.7")
    create(hub, "198.51.100.8")


def test_the_global_limit_of_open_pairings(hub: HubHarness) -> None:
    now = hub.clock()
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, source, created_at, expires_at)"
            " SELECT gen_random_uuid(), decode(lpad(to_hex(i), 64, '0'), 'hex'), decode(lpad(to_hex(i), 64, '0'),"
            " 'hex'), 'open', 's' || i, %s, %s FROM generate_series(1, %s) AS i",
            (now, now + timedelta(seconds=600), store.MAX_OPEN_PAIRINGS),
        )
    response = hub.anon.post("/v1/agent-pairings", json=new_agent(hub).pairing_body())
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})


# --- GET /v1/agent-pairings/{id} --------------------------------------------------------------------------------------


def test_a_pairing_is_shown_only_while_open_or_confirmed_before_its_deadline(hub: HubHarness) -> None:
    agent = paired(hub)
    response = hub.anon.get(f"/v1/agent-pairings/{agent.agent_id}")
    assert response.status_code == 200
    assert response.json() == {
        "pairing_id": str(agent.agent_id),
        "pub": agent.identity.public(),
        "state": "open",
        "expires_at": format_time(hub.clock() + timedelta(seconds=600)),
    }
    confirmed = stored_agent(hub, state="confirmed")
    assert hub.anon.get(f"/v1/agent-pairings/{confirmed.agent_id}").json()["state"] == "confirmed"
    for state in ("active", "revoked"):
        gone = stored_agent(hub, state=state)
        response = hub.anon.get(f"/v1/agent-pairings/{gone.agent_id}")
        assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})
    for text in (str(uuid7(hub.clock())), str(uuid.uuid4()), "nope", str(agent.agent_id).upper()):
        response = hub.anon.get(f"/v1/agent-pairings/{text}")
        assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})
    hub.clock.advance(600)
    for pairing in (agent, confirmed):
        assert hub.anon.get(f"/v1/agent-pairings/{pairing.agent_id}").status_code == 404


def test_public_state() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    later = moment + timedelta(seconds=1)
    assert store.public_state("active", moment, later) == "active"
    assert store.public_state("open", later, moment) == "open"
    assert store.public_state("confirmed", moment, later) == "closed"
    assert store.public_state("revoked", later, moment) == "closed"


# --- login with a pairing ---------------------------------------------------------------------------------------------


def test_login_begin_with_a_pairing_tags_the_ceremony_and_the_session(browser: FakeBrowser) -> None:
    hub = browser.hub
    account = browser.register(seed_invite(hub))
    agent = paired(hub)
    begin = hub.anon.post("/v1/passkeys/login/begin", json={"pairing_id": str(agent.agent_id)})
    assert begin.status_code == 200, begin.text
    ceremony = begin.json()
    assert rows(hub, "SELECT pairing_id FROM hub.ceremony WHERE id = %s", ceremony["ceremony_id"]) == [
        (agent.agent_id,)
    ]
    hub_value = b64u_decode(ceremony["hub_value"])
    certificate, private, _statement = browser.certificate(hub_value, account.credential_id)
    finish = hub.anon.post(
        "/v1/passkeys/login/finish", json={"ceremony_id": ceremony["ceremony_id"], "certificate": certificate}
    )
    assert finish.status_code == 200, finish.text
    key_id = finish.json()["key_id"]
    assert rows(hub, "SELECT pairing_id FROM hub.browser_session WHERE key_id = %s", key_id) == [(agent.agent_id,)]
    session = browser.client(Session(key_id, private, finish.json(), account.user_id))
    confirm = handover(agent, account.user_id, account.identity)
    response = session.post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(agent.agent_id)})
    # a login without a pairing creates an untagged session that cannot confirm
    plain = browser.client(browser.login(account))
    second = paired(hub)
    refused = plain.post("/v1/me/agents", json=handover(second, account.user_id, account.identity).body)
    assert (refused.status_code, refused.json()) == (403, {"error": "pairing_gesture_required"})


def test_login_begin_with_an_unknown_or_closed_pairing_is_refused(hub: HubHarness) -> None:
    confirmed = stored_agent(hub, state="confirmed")
    expired = paired(hub)
    hub.clock.advance(600)
    for pairing in (uuid7(hub.clock()), confirmed.agent_id, expired.agent_id):
        response = hub.anon.post("/v1/passkeys/login/begin", json={"pairing_id": str(pairing)})
        assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})
    assert rows(hub, "SELECT count(*) FROM hub.ceremony") == [(0,)]
    assert hub.anon.post("/v1/passkeys/login/begin", json={}).status_code == 200


# --- POST /v1/me/agents -----------------------------------------------------------------------------------------------


def test_the_gesture_session_confirms_within_300_seconds(hub: HubHarness, published: MemoryPublisher) -> None:
    user, identity = user_with_identity(hub)
    agent = paired(hub)
    confirm = handover(agent, user, identity)
    late = gesture_client(hub, user, agent.agent_id, age=301)
    response = late.post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (403, {"error": "pairing_gesture_required"})
    other = gesture_client(hub, user, uuid7(hub.clock()))
    assert other.post("/v1/me/agents", json=confirm.body).json() == {"error": "pairing_gesture_required"}
    session = gesture_client(hub, user, agent.agent_id, age=300)
    response = session.post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(agent.agent_id)})
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["user_id"], row["source"], row["replaces"]) == ("confirmed", user, None, None)
    assert row["confirmed_at"] == hub.clock()
    assert bytes(row["claim_hash"]) == agent_format.claim_hash(confirm.claim)
    assert rows(hub, "SELECT action, actor_id, target_kind, target_id, agent_id FROM hub.audit") == [
        ("agent.confirm", user, "agent", str(agent.agent_id), None)
    ]
    assert published.items() == []  # a passkey gesture notifies nobody
    # an identical repeat is idempotent, other values are a conflict
    assert session.post("/v1/me/agents", json=confirm.body).json() == {"agent_id": str(agent.agent_id)}
    changed = handover(agent, user, identity)
    response = session.post("/v1/me/agents", json=changed.body)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert rows(hub, "SELECT count(*) FROM hub.audit WHERE action = 'agent.confirm'") == [(1,)]


def test_only_an_open_pairing_before_its_deadline_is_confirmed(hub: HubHarness) -> None:
    user, identity = user_with_identity(hub)
    stranger, stranger_identity = user_with_identity(hub)
    agent = paired(hub)
    taken = handover(agent, stranger, stranger_identity)
    assert gesture_client(hub, stranger, agent.agent_id).post("/v1/me/agents", json=taken.body).status_code == 200
    # confirmed by another user: unknown to this one
    response = gesture_client(hub, user, agent.agent_id).post(
        "/v1/me/agents", json=handover(agent, user, identity).body
    )
    assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})
    missing = new_agent(hub)
    response = gesture_client(hub, user, missing.agent_id).post(
        "/v1/me/agents", json=handover(missing, user, identity).body
    )
    assert response.json() == {"error": "pairing_unknown"}
    late = paired(hub)
    hub.clock.advance(600)
    session = gesture_client(hub, user, late.agent_id)
    response = session.post("/v1/me/agents", json=handover(late, user, identity).body)
    assert response.json() == {"error": "pairing_unknown"}


def test_too_short_sealed_values_are_request_invalid(hub: HubHarness) -> None:
    user, identity = user_with_identity(hub)
    agent = paired(hub)
    body = handover(agent, user, identity).body
    session = gesture_client(hub, user, agent.agent_id)
    for field in ("bundle_ct", "label_ct"):
        response = session.post("/v1/me/agents", json={**body, field: b64u_encode(bytes(47))})
        assert (response.status_code, response.json()) == (
            400,
            {"error": "request_invalid", "detail": {"pointers": [f"/{field}"]}},
        )
    assert session.post("/v1/me/agents", json={**body, "bundle_ct": b64u_encode(bytes(48))}).status_code == 200


def test_a_new_confirmation_replaces_the_main_orchestrator(hub: HubHarness, published: MemoryPublisher) -> None:
    """Concept idea 34: one main orchestrator per user. The passkey confirmation of a new pairing revokes the previous
    (active or confirmed) agent in the same transaction, ends its streams and names it in `replaces`."""
    user, identity = user_with_identity(hub)
    first = stored_agent(hub, user)
    second = paired(hub)
    confirm = handover(second, user, identity)
    session = gesture_client(hub, user, second.agent_id)
    response = session.post("/v1/me/agents", json=confirm.body)
    assert (response.status_code, response.json()) == (200, {"agent_id": str(second.agent_id)})
    old, new = agent_row(hub, first.agent_id), agent_row(hub, second.agent_id)
    assert old is not None
    assert new is not None
    assert (old["state"], old["bundle_ct"], old["revoked_at"]) == ("revoked", None, hub.clock())
    assert (new["state"], new["replaces"]) == ("confirmed", first.agent_id)
    assert first.client(hub).get("/v1/agent/me").json() == {"error": "key_revoked"}
    assert rows(hub, "SELECT action, target_id, agent_id FROM hub.audit ORDER BY seq") == [
        ("agent.revoke", str(first.agent_id), None),
        ("agent.confirm", str(second.agent_id), None),
    ]
    assert [(item.frame, item.data) for item in published.items()] == [("close_keys", {"key_ids": [first.key_id]})]
    listed = session.get("/v1/me/agents").json()["agents"]
    assert [(entry["agent_id"], entry["state"], entry["replaces"]) for entry in listed] == [
        (str(second.agent_id), "confirmed", str(first.agent_id)),
        (str(first.agent_id), "revoked", None),
    ]
    # the repeat replaces nothing again; a third pairing replaces the confirmed (not yet claimed) second one
    assert session.post("/v1/me/agents", json=confirm.body).status_code == 200
    third = paired(hub)
    later = gesture_client(hub, user, third.agent_id)
    assert later.post("/v1/me/agents", json=handover(third, user, identity).body).status_code == 200
    states = rows(hub, "SELECT id, state, replaces FROM hub.agent WHERE user_id = %s ORDER BY created_at", user)
    assert sorted(states, key=lambda row: str(row[0])) == sorted(
        [
            (first.agent_id, "revoked", None),
            (second.agent_id, "revoked", first.agent_id),
            (third.agent_id, "confirmed", second.agent_id),
        ],
        key=lambda row: str(row[0]),
    )
    assert rows(hub, "SELECT count(*) FROM hub.notification") == [(0,)]  # the passkey gesture notifies nobody


def test_an_agent_never_confirms_a_pairing(hub: HubHarness) -> None:
    user, identity = user_with_identity(hub)
    first = stored_agent(hub, user)
    second = paired(hub)
    response = first.client(hub).post("/v1/me/agents", json=handover(second, user, identity).body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    row = agent_row(hub, second.agent_id)
    assert row is not None
    assert row["state"] == "open"
    assert first.client(hub).get("/v1/agent/me").status_code == 200


def test_two_main_orchestrators_at_once_are_impossible(hub: HubHarness) -> None:
    user = stored_agent(hub).user_id
    with pytest.raises(psycopg.errors.UniqueViolation):
        stored_agent(hub, user, state="confirmed")


def test_concurrent_confirmations_of_one_user_leave_exactly_one_main_orchestrator(
    hub: HubHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two confirmations of one user at once: the second waits for the first (the user's lock), then replaces it."""
    monkeypatch.setattr(store, "gesture_session", lambda *_args: None)  # the gesture is tested above
    user, identity = user_with_identity(hub)
    first, second = paired(hub), paired(hub)

    def body(agent: Any) -> store.Confirmation:
        sent = handover(agent, user, identity).body
        return store.Confirmation(
            pairing_id=agent.agent_id,
            bundle_ct=b64u_decode(sent["bundle_ct"]),
            label_ct=b64u_decode(sent["label_ct"]),
            claim_hash=b64u_decode(sent["claim_hash"]),
        )

    holding, results = threading.Event(), []
    with hub.pool.connection() as one:
        results.append(store.confirm(one, user, "session.x", body(first), hub.clock()))

        def other() -> None:
            with hub.pool.connection() as two:
                holding.set()
                results.append(store.confirm(two, user, "session.y", body(second), hub.clock()))

        waiting = threading.Thread(target=other)
        waiting.start()
        assert holding.wait(5)
        waiting.join(0.5)
        assert waiting.is_alive()  # blocked on the user's lock until the first transaction commits
    waiting.join(10)
    assert not waiting.is_alive()
    assert results == [store.Confirmed(changed=True), store.Confirmed(changed=True, replaced=(first.agent_id,))]
    states = rows(hub, "SELECT id, state, replaces FROM hub.agent WHERE user_id = %s", user)
    assert sorted(states, key=lambda row: str(row[0])) == sorted(
        [(first.agent_id, "revoked", None), (second.agent_id, "confirmed", first.agent_id)],
        key=lambda row: str(row[0]),
    )


# --- POST /v1/agent/claim ---------------------------------------------------------------------------------------------


def confirmed_agent(hub: HubHarness) -> tuple[TestAgent, Any, uuid.UUID, Identity]:
    user, identity = user_with_identity(hub)
    agent = paired(hub)
    confirm = handover(agent, user, identity)
    assert gesture_client(hub, user, agent.agent_id).post("/v1/me/agents", json=confirm.body).status_code == 200
    agent.user_id = user
    return agent, confirm, user, identity


def test_the_claim_activates_the_agent_which_opens_the_identity(hub: HubHarness) -> None:
    agent, confirm, user, identity = confirmed_agent(hub)
    client = agent.client(hub)
    # a confirmed agent may only claim
    assert (client.get("/v1/agent/me").status_code, client.get("/v1/me").status_code) == (403, 403)
    hub.clock.advance(30)
    response = client.post("/v1/agent/claim", json=confirm.claim_body)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "agent_id": str(agent.agent_id),
        "user_id": str(user),
        "bundle_ct": confirm.body["bundle_ct"],
        "label_ct": confirm.body["label_ct"],
        "created_at": format_time(hub.clock() - timedelta(seconds=30)),
        "claimed_at": format_time(hub.clock()),
    }
    opened = agent_format.open_identity(
        agent.identity.kem, confirm.secret, str(agent.agent_id), str(user), body["bundle_ct"]
    )
    assert opened.public() == identity.public()
    assert agent_format.open_label(identity.kem, str(agent.agent_id), str(user), body["label_ct"]) == {
        "host": "Büro-Mac",
        "name": "Claude Code",
    }
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["claimed_at"], row["last_used_at"]) == ("active", hub.clock(), hub.clock())
    assert rows(hub, "SELECT action, actor_id, agent_id FROM hub.audit ORDER BY seq")[-1] == (
        "agent.claim",
        user,
        agent.agent_id,
    )
    # a repeat when active with the right claim gives the same answer; GET /v1/agent/me too
    hub.clock.advance(5)
    assert client.post("/v1/agent/claim", json=confirm.claim_body).json() == body
    assert client.get("/v1/agent/me").json() == body
    assert rows(hub, "SELECT count(*) FROM hub.audit WHERE action = 'agent.claim'") == [(1,)]


def test_wrong_claims_are_counted_and_the_fifth_deletes_the_pairing(hub: HubHarness) -> None:
    agent, confirm, _user, _identity = confirmed_agent(hub)
    client = agent.client(hub)
    wrong = {"claim": b64u_encode(bytes(32))}
    for misses in range(1, 5):
        response = client.post("/v1/agent/claim", json=wrong)
        assert (response.status_code, response.json()) == (401, {"error": "claim_invalid"})
        assert response.extensions["hub_signature"] is True
        row = agent_row(hub, agent.agent_id)
        assert row is not None
        assert row["claim_misses"] == misses
    assert client.post("/v1/agent/claim", json=wrong).json() == {"error": "claim_invalid"}
    assert agent_row(hub, agent.agent_id) is None
    response = client.post("/v1/agent/claim", json=confirm.claim_body)
    assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})


def test_wrong_claims_of_an_active_agent_never_delete_it(hub: HubHarness) -> None:
    agent, confirm, _user, _identity = confirmed_agent(hub)
    client = agent.client(hub)
    assert client.post("/v1/agent/claim", json=confirm.claim_body).status_code == 200
    for _ in range(6):
        assert client.post("/v1/agent/claim", json={"claim": b64u_encode(bytes(32))}).status_code == 401
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["claim_misses"]) == ("active", 6)
    assert client.get("/v1/agent/me").status_code == 200


def test_a_claim_past_the_deadline_is_unknown(hub: HubHarness) -> None:
    agent, confirm, _user, _identity = confirmed_agent(hub)
    hub.clock.advance(600)
    response = agent.client(hub).post("/v1/agent/claim", json=confirm.claim_body)
    assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})
    with hub.pool.connection() as connection, connection.transaction(), pytest.raises(HubError) as refused:
        store.claim(connection, agent.agent_id, confirm.claim, hub.clock())
    assert refused.value.code == "pairing_unknown"


def test_claim_and_me_of_rows_that_are_not_there(hub: HubHarness) -> None:
    revoked = stored_agent(hub, state="revoked")
    with hub.pool.connection() as connection:
        for agent_id in (revoked.agent_id, uuid7(hub.clock())):
            with pytest.raises(HubError) as refused:
                store.claim(connection, agent_id, bytes(32), hub.clock())
            assert refused.value.code == "pairing_unknown"
        with pytest.raises(HubError) as refused:
            store.me(connection, revoked.agent_id)
        assert refused.value.code == "key_revoked"
        with pytest.raises(HubError) as refused:
            store.get_pairing(connection, uuid7(hub.clock()))
        assert refused.value.code == "pairing_unknown"
