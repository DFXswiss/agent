"""U-HUB-agents (list and revocation, 11-agent-access.md sections 4 and 5.3, ADR-056 section 5): `GET /v1/me/agents`,
`DELETE /v1/me/agents/{id}` and `DELETE /v1/me/agents` by the user's browser session or by an agent of the user. A
revoked agent is refused at once with a response-signed `401 key_revoked`, its sealed identity is deleted, and the user
is notified when an agent (not a passkey session) revoked."""

from __future__ import annotations

import uuid
from datetime import timedelta

from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.agents.support import agent_row, rows, stored_agent
from tests.platform.hub.conftest import HubHarness, uuid7


def test_the_list_shows_every_agent_of_the_user_newest_first(hub: HubHarness) -> None:
    user = uuid7(hub.clock())
    oldest = stored_agent(hub, user, state="revoked")
    hub.clock.advance(1)
    replaced = stored_agent(hub, user, state="revoked")
    hub.clock.advance(1)
    active = stored_agent(hub, user)
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.agent SET replaces = %s WHERE id = %s", (replaced.agent_id, active.agent_id))
    stored_agent(hub)  # another user's
    stored_agent(hub, state="open")
    hub.clock.advance(40)
    with hub.pool.connection() as connection:
        connection.execute("UPDATE hub.agent SET last_used_at = %s WHERE id = %s", (hub.clock(), active.agent_id))
    response = hub.browser(user).get("/v1/me/agents")
    assert response.status_code == 200
    agents = response.json()["agents"]
    assert [(entry["agent_id"], entry["state"]) for entry in agents] == [
        (str(active.agent_id), "active"),
        (str(replaced.agent_id), "revoked"),
        (str(oldest.agent_id), "revoked"),
    ]
    row = agent_row(hub, active.agent_id)
    assert row is not None
    assert agents[0] == {
        "agent_id": str(active.agent_id),
        "state": "active",
        "label_ct": b64u_encode(bytes(row["label_ct"])),
        "created_at": format_time(row["created_at"]),
        "confirmed_at": format_time(row["confirmed_at"]),
        "claimed_at": format_time(row["claimed_at"]),
        "last_used_at": format_time(hub.clock()),
        "revoked_at": None,
        "replaces": str(replaced.agent_id),
        "remote": False,
        # paired before ADR-082: it holds the user's identity
        "own_identity": False,
        "held_identity": True,
        "gen": None,
        "revoked_reason": None,
        "pub": active.identity.public(),
        "cert": None,
        "session_id": None,
    }
    assert agents[2]["revoked_at"] is not None
    assert agents[2]["replaces"] is None
    # an agent of the user sees the same list; the read is logged
    assert active.client(hub).get("/v1/me/agents").json() == {"agents": agents}
    hub.flush_read_log()
    assert rows(
        hub, "SELECT action, actor_id, agent_id FROM hub.audit WHERE action = 'read.me.agents' ORDER BY seq"
    ) == [
        ("read.me.agents", user, None),
        ("read.me.agents", user, active.agent_id),
    ]


def test_an_unclaimed_agent_past_its_deadline_is_listed_as_expired(hub: HubHarness) -> None:
    expired = stored_agent(hub, state="confirmed", expires_in=timedelta(seconds=30))
    confirmed = stored_agent(hub, state="confirmed")
    hub.clock.advance(40)
    for agent, state in ((expired, "expired"), (confirmed, "confirmed")):
        assert agent.user_id is not None
        (entry,) = hub.browser(agent.user_id).get("/v1/me/agents").json()["agents"]
        assert (entry["agent_id"], entry["state"], entry["claimed_at"]) == (str(agent.agent_id), state, None)


def test_the_session_revokes_one_agent_at_once(hub: HubHarness, published: MemoryPublisher) -> None:
    agent = stored_agent(hub)
    user = agent.user_id
    assert user is not None
    client = agent.client(hub)
    assert client.get("/v1/agent/me").status_code == 200
    hub.clock.advance(3)
    session = hub.browser(user)
    response = session.delete(f"/v1/me/agents/{agent.agent_id}")
    assert (response.status_code, response.json()) == (200, {})
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["revoked_at"], row["bundle_ct"]) == ("revoked", hub.clock(), None)
    refused = client.get("/v1/agent/me")
    assert (refused.status_code, refused.json()) == (401, {"error": "key_revoked"})
    assert refused.extensions["hub_signature"] is True
    assert client.get("/v1/me").json() == {"error": "key_revoked"}
    writes = "SELECT action, actor_id, target_kind, target_id, agent_id FROM hub.audit WHERE action NOT LIKE 'read.%%'"
    assert rows(hub, writes) == [("agent.revoke", user, "agent", str(agent.agent_id), None)]
    # a session's revocation notifies nobody; a repeat changes nothing (the pipeline's generic row records it)
    assert rows(hub, "SELECT count(*) FROM hub.notification") == [(0,)]
    assert session.delete(f"/v1/me/agents/{agent.agent_id}").json() == {}
    assert rows(hub, writes) == [
        ("agent.revoke", user, "agent", str(agent.agent_id), None),
        ("request.write", user, "agent_id", str(agent.agent_id), None),
    ]
    # the agent's open streams are ended in every hub process (no notification frame)
    assert [(item.audience.id, item.frame, item.data) for item in published.items()] == [
        (user, "close_keys", {"key_ids": [agent.key_id]})
    ]


def test_unknown_foreign_and_malformed_agents_are_not_found(hub: HubHarness) -> None:
    mine, foreign = stored_agent(hub), stored_agent(hub)
    assert mine.user_id is not None
    session = hub.browser(mine.user_id)
    for target in (foreign.agent_id, uuid7(hub.clock()), uuid.uuid4(), "nope", str(mine.agent_id).upper()):
        response = session.delete(f"/v1/me/agents/{target}")
        assert (response.status_code, response.json()) == (404, {"error": "not_found"}), target
    assert foreign.client(hub).get("/v1/agent/me").status_code == 200


def test_the_session_revokes_every_agent_at_once(hub: HubHarness) -> None:
    user = uuid7(hub.clock())
    revoked = stored_agent(hub, user, state="revoked")
    agents = [stored_agent(hub, user)]
    other = stored_agent(hub)
    session = hub.browser(user)
    response = session.delete("/v1/me/agents")
    assert (response.status_code, response.json()) == (200, {"revoked": 1})
    for agent in agents:
        row = agent_row(hub, agent.agent_id)
        assert row is not None
        assert (row["state"], row["bundle_ct"]) == ("revoked", None)
        assert agent.client(hub).get("/v1/agent/me").json() == {"error": "key_revoked"}
    assert rows(hub, "SELECT count(*) FROM hub.audit WHERE action = 'agent.revoke'") == [(1,)]
    assert agent_row(hub, revoked.agent_id) is not None
    assert other.client(hub).get("/v1/agent/me").status_code == 200
    assert session.delete("/v1/me/agents").json() == {"revoked": 0}
    assert rows(hub, "SELECT count(*) FROM hub.notification") == [(0,)]


def test_an_agent_disconnecting_itself_notifies_the_user(hub: HubHarness, published: MemoryPublisher) -> None:
    actor = stored_agent(hub)
    user = actor.user_id
    assert user is not None
    earlier = stored_agent(hub, user, state="revoked")
    client = actor.client(hub)
    # an earlier (revoked) agent: nothing changes, nobody is notified
    assert client.delete(f"/v1/me/agents/{earlier.agent_id}").json() == {}
    assert rows(hub, "SELECT count(*) FROM hub.notification") == [(0,)]
    response = client.delete("/v1/me/agents")
    assert (response.status_code, response.json()) == (200, {"revoked": 1})
    assert response.extensions["hub_signature"] is True
    assert client.get("/v1/agent/me").json() == {"error": "key_revoked"}
    assert rows(hub, "SELECT action, target_id, agent_id FROM hub.audit WHERE action NOT LIKE 'read.%%'") == [
        ("request.write", str(earlier.agent_id), actor.agent_id),
        ("agent.revoke", str(actor.agent_id), actor.agent_id),
    ]
    ref = f"agent_revoked:{actor.agent_id}:{actor.agent_id}"
    assert rows(hub, "SELECT recipient_id, kind, org_id, ref FROM hub.notification") == [
        (user, "agent.account_change", None, ref)
    ]
    notice, close = published.items()
    assert (notice.audience.id, notice.frame, notice.data["kind"], notice.data["org_id"]) == (
        user,
        "notification",
        "agent.account_change",
        None,
    )
    assert (close.frame, close.data) == ("close_keys", {"key_ids": [actor.key_id]})
    # the notification is in the user's list with `account=1` only (no organization; old clients never see it)
    session = hub.browser(user)
    assert session.get("/v1/notifications").json()["notifications"] == []
    listed = session.get("/v1/notifications", params={"account": "1"}).json()
    assert [(entry["kind"], entry["org_id"], entry["ref"]) for entry in listed["notifications"]] == [
        ("agent.account_change", None, ref)
    ]


def test_an_agent_revokes_itself(hub: HubHarness) -> None:
    agent = stored_agent(hub)
    client = agent.client(hub)
    response = client.delete(f"/v1/me/agents/{agent.agent_id}")
    assert (response.status_code, response.json()) == (200, {})
    assert client.delete(f"/v1/me/agents/{agent.agent_id}").json() == {"error": "key_revoked"}
