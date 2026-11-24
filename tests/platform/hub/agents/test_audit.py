"""U-HUB-agents (audit, 11-agent-access.md section 7): every audit and read-log row written while an agent's request is
processed carries `agent_id`; the hashed row JSON contains the key only when it is set, so the hashes of rows without
an agent are those of before KOR-24 and a chain of mixed rows verifies; `GET /v1/orgs/{org}/audit` returns `agent_id`;
`GET /v1/me/audit` returns the user's account audit and everything the user's agents did."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from agent_hub.audit.chain import ACTING_AGENT, AuditRow, append_audit, audit_hash, row_json, verify_chain
from tests.platform.hub.agents.support import rows, stored_agent
from tests.platform.hub.conftest import HubHarness, uuid7
from tests.platform.hub.orgs.support import World

AT = datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)


def test_a_row_without_an_agent_hashes_as_before() -> None:
    row = AuditRow(at=AT, actor_kind="hub", action="invite.release", result="ok", detail={"kind": "user"})
    before = {
        "seq": 7,
        "at": "2026-11-21T12:00:00.123Z",
        "actor_id": None,
        "actor_kind": "hub",
        "action": "invite.release",
        "result": "ok",
        "org_id": None,
        "wg_id": None,
        "task_id": None,
        "session_id": None,
        "device_id": None,
        "target_kind": None,
        "target_id": None,
        "detail": {"kind": "user"},
        "event_id": None,
    }
    assert row_json(7, row) == before
    text = "\n".join(["agent-audit-v1", "7", json.dumps(before, sort_keys=True, separators=(",", ":")), "-"])
    assert audit_hash(7, row, None) == hashlib.sha256(text.encode()).hexdigest()
    agent = uuid.UUID("01930050-0000-7000-8000-000000000050")
    with_agent = replace(row, agent_id=agent)
    assert row_json(7, with_agent) == {**before, "agent_id": str(agent)}
    assert audit_hash(7, with_agent, None) != audit_hash(7, row, None)


def test_append_takes_the_acting_agent_and_a_mixed_chain_verifies(hub: HubHarness) -> None:
    agent = uuid7(hub.clock())
    with hub.pool.connection() as connection:
        plain = append_audit(connection, AuditRow(at=AT, actor_kind="user", action="read.me", result="ok"))
        token = ACTING_AGENT.set(agent)
        try:
            acted = append_audit(connection, AuditRow(at=AT, actor_kind="user", action="read.me", result="ok"))
            other = uuid7(hub.clock())
            named = append_audit(
                connection, AuditRow(at=AT, actor_kind="user", action="read.me", result="ok", agent_id=other)
            )
        finally:
            ACTING_AGENT.reset(token)
        assert (plain.row.agent_id, acted.row.agent_id, named.row.agent_id) == (None, agent, other)
        check = verify_chain(connection)
    assert check.ok
    assert check.head_seq == 3
    assert rows(hub, "SELECT agent_id FROM hub.audit ORDER BY seq") == [(None,), (agent,), (other,)]


def test_the_org_audit_shows_what_the_agent_did_and_verifies(hub: HubHarness) -> None:
    world = World(hub)
    founder = world.user()
    agent = stored_agent(hub, uuid.UUID(founder.id))
    draft = world.org(replace(founder, client=agent.client(hub)))
    assert draft.create().status_code == 200
    assert founder.browser.get(f"/v1/orgs/{draft.id}").status_code == 200  # a browser row of the same user
    hub.flush_read_log()
    response = founder.browser.get(f"/v1/orgs/{draft.id}/audit")
    assert response.status_code == 200
    found = response.json()["rows"]
    by_agent = [row for row in found if row.get("agent_id") == str(agent.agent_id)]
    by_browser = [row for row in found if "agent_id" not in row]
    assert by_agent
    assert by_browser
    assert {row["actor_id"] for row in found if row["actor_kind"] == "user"} == {founder.id}
    verify = founder.browser.get(f"/v1/orgs/{draft.id}/audit/verify").json()
    assert verify["ok"] is True
    # the agent's own read of the audit is logged with its ID
    assert agent.client(hub).get(f"/v1/orgs/{draft.id}/audit").status_code == 200
    hub.flush_read_log()
    assert rows(hub, "SELECT agent_id FROM hub.audit WHERE action = 'read.org.audit' ORDER BY seq") == [
        (None,),
        (agent.agent_id,),
    ]


def test_the_account_audit_holds_the_users_rows_and_everything_its_agents_did(hub: HubHarness) -> None:
    world = World(hub)
    founder = world.user()
    agent = stored_agent(hub, uuid.UUID(founder.id))
    draft = world.org(replace(founder, client=agent.client(hub)))
    assert draft.create().status_code == 200
    session = founder.browser
    stranger = stored_agent(hub)
    assert stranger.client(hub).delete(f"/v1/me/agents/{stranger.agent_id}").status_code == 200
    hub.flush_read_log()
    # the rows before the listing (its own read row is written before its answer leaves, after the query)
    expected = rows(
        hub,
        "SELECT seq FROM hub.audit WHERE (actor_id = %s AND org_id IS NULL) OR agent_id = %s ORDER BY seq DESC",
        founder.id,
        agent.agent_id,
    )
    response = session.get("/v1/me/audit")
    assert response.status_code == 200
    body = response.json()
    seqs = [row["seq"] for row in body["rows"]]
    assert seqs == sorted(seqs, reverse=True)
    assert seqs == [seq for (seq,) in expected]
    assert any(row.get("org_id") == draft.id and row.get("agent_id") == str(agent.agent_id) for row in body["rows"])
    assert all(row.get("agent_id") != str(stranger.agent_id) for row in body["rows"])
    assert body["next_before"] is None

    # paging (every listing's own read row is newer than what it lists)
    def account_rows() -> list[int]:
        found = rows(
            hub,
            "SELECT seq FROM hub.audit WHERE (actor_id = %s AND org_id IS NULL) OR agent_id = %s ORDER BY seq DESC",
            founder.id,
            agent.agent_id,
        )
        return [seq for (seq,) in found]

    latest = account_rows()
    first = session.get("/v1/me/audit", params={"limit": "1"}).json()
    assert [row["seq"] for row in first["rows"]] == latest[:1]
    second = session.get("/v1/me/audit", params={"limit": "1", "before": first["next_before"]}).json()
    assert [row["seq"] for row in second["rows"]] == latest[1:2]
    # the agent reads the same account audit (plus its own read rows)
    assert agent.client(hub).get("/v1/me/audit").status_code == 200
    hub.flush_read_log()
    assert rows(hub, "SELECT count(*) FROM hub.audit WHERE action = 'read.me.audit'") == [(4,)]
    # the user's revocation of the agent is an account row
    assert session.delete(f"/v1/me/agents/{agent.agent_id}").json() == {}
    assert "agent.revoke" in {row["action"] for row in session.get("/v1/me/audit").json()["rows"]}
