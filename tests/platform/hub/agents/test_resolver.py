"""U-HUB-agents (key resolver, 11-agent-access.md section 4): `agent.<32 hex>` keys resolve to a principal of kind
`agent` acting for its user; unknown, open and expired confirmed agents are `key_unknown`, revoked agents and agents
of a disabled user `key_revoked` (response-signed); `last_used_at` is written only after a verified signature, at
most once a minute; agents share the rate limit of a browser session."""

from __future__ import annotations

from datetime import timedelta

from agent_hub.agents.keys import LAST_USED_SECONDS, key_id_of
from agent_hub.crypto import agent as agent_format
from agent_hub.http.limits import PER_MINUTE, SESSION_PER_MINUTE
from agent_hub.http.pipeline import key_kind
from tests.platform.hub.agents.support import TestAgent, agent_row, rows, stored_agent
from tests.platform.hub.conftest import HubHarness, SigningKey, uuid7


def test_the_key_id_is_the_agent_id_without_dashes(hub: HubHarness) -> None:
    agent_id = uuid7(hub.clock())
    assert key_id_of(agent_id) == "agent." + agent_id.hex
    assert key_id_of(str(agent_id)) == key_id_of(agent_id)
    assert key_kind(key_id_of(agent_id)) == "agent"
    assert PER_MINUTE["agent"] == SESSION_PER_MINUTE == 600


def test_an_active_agent_acts_for_its_user(hub: HubHarness) -> None:
    agent = stored_agent(hub)
    assert agent.user_id is not None
    response = agent.client(hub).get("/v1/me/agents")
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True
    hub.flush_read_log()
    assert rows(hub, "SELECT actor_kind, actor_id, agent_id FROM hub.audit") == [
        ("user", agent.user_id, agent.agent_id)
    ]


def test_unknown_open_and_expired_agents_are_unknown_keys(hub: HubHarness) -> None:
    unknown = TestAgent(identity=agent_format.new_agent(), agent_id=uuid7(hub.clock()), user_id=None)
    opened = stored_agent(hub, state="open")
    confirmed = stored_agent(hub, state="confirmed", expires_in=timedelta(seconds=10))
    for agent in (unknown, opened):
        response = agent.client(hub).post("/v1/agent/claim", json={"claim": "A" * 43})
        assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})
        assert response.extensions["hub_signature"] is None
    assert confirmed.client(hub).post("/v1/agent/claim", json={"claim": "A" * 43}).status_code == 401
    assert confirmed.client(hub).post("/v1/agent/claim", json={"claim": "A" * 43}).json() == {"error": "claim_invalid"}
    hub.clock.advance(10)
    assert confirmed.client(hub).post("/v1/agent/claim", json={"claim": "A" * 43}).json() == {"error": "key_unknown"}


def test_revoked_agents_and_disabled_users_are_revoked_keys(hub: HubHarness) -> None:
    revoked = stored_agent(hub, state="revoked")
    active, confirmed = stored_agent(hub), stored_agent(hub, state="confirmed")
    with hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.principal SET disabled_at = %s WHERE id = ANY(%s)",
            (hub.clock(), [active.user_id, confirmed.user_id]),
        )
    for agent in (revoked, active):
        response = agent.client(hub).get("/v1/agent/me")
        assert (response.status_code, response.json()) == (401, {"error": "key_revoked"})
        assert response.extensions["hub_signature"] is True
    response = confirmed.client(hub).post("/v1/agent/claim", json={"claim": "A" * 43})
    assert response.json() == {"error": "key_revoked"}
    row = agent_row(hub, active.agent_id)
    assert row is not None
    assert row["last_used_at"] is None


def test_last_used_is_written_after_a_verified_signature_at_most_once_a_minute(hub: HubHarness) -> None:
    agent = stored_agent(hub)
    client = agent.client(hub)

    def last_used() -> object:
        row = agent_row(hub, agent.agent_id)
        assert row is not None
        return row["last_used_at"]

    # a forged signature with the agent's key ID writes nothing
    forger = agent_format.new_agent()
    forged = hub.client(SigningKey(key_id=agent.key_id, sign=forger.sig.sign, principal=agent.signing_key().principal))
    response = forged.get("/v1/agent/me", extensions={"contract": False})
    assert (response.status_code, response.json()) == (401, {"error": "signature_invalid"})
    assert last_used() is None
    # a request the route refuses (a WebAuthn ceremony) writes nothing either
    assert client.post("/v1/me/recovery/begin", json={}).json() == {"error": "forbidden"}
    assert last_used() is None
    first = hub.clock()
    assert client.get("/v1/agent/me").status_code == 200
    assert last_used() == first
    hub.clock.advance(LAST_USED_SECONDS - 1)
    assert client.get("/v1/agent/me").status_code == 200
    assert last_used() == first
    hub.clock.advance(1)
    assert client.get("/v1/agent/me").status_code == 200
    assert last_used() == hub.clock()


def test_agents_have_the_rate_limit_of_a_browser_session(hub: HubHarness) -> None:
    agent = stored_agent(hub)
    limiter = hub.app.state.hub.limiter
    for _ in range(SESSION_PER_MINUTE):
        assert limiter.hit("agent", agent.key_id) is None
    response = agent.client(hub).get("/v1/agent/me")
    assert (response.status_code, response.json()) == (429, {"error": "rate_limited"})
    assert response.headers["retry-after"] == "1"
    # a revoked agent's refusals count against its own key
    revoked = stored_agent(hub, state="revoked")
    for _ in range(SESSION_PER_MINUTE):
        limiter.hit("agent", revoked.key_id)
    assert revoked.client(hub).get("/v1/agent/me").json() == {"error": "rate_limited"}
