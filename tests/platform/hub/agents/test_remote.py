"""U-HUB-agents (pairing from another computer, ADR-067, 11-agent-access.md sections 2, 4 and 5.4): a remote pairing
has the hub's nonce and a 300 s confirmation deadline; its confirmation needs `handover_ct` (a loopback one refuses
it), notifies the user (`agent_paired_remote`), records `remote` in the audit and gives the agent 600 s to fetch the
handover (`GET /v1/agent/handover`, only for a confirmed remote row) and claim; the handover is cleared at the claim,
the revocation and the claim deadline; `GET /v1/me/agents` names remote rows; loopback pairings and old clients are
answered as before; migration 0017 applies on the schema of 0016 with data."""

from __future__ import annotations

import secrets
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_hub.agents import jobs as agent_jobs
from agent_hub.agents import notify, store
from agent_hub.audit.details import detail_violation
from agent_hub.crypto import agent as agent_format
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.keys import Identity
from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from agent_hub.errors import HubError
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.agents.support import (
    Handover,
    TestAgent,
    agent_row,
    gesture_client,
    handover,
    new_agent,
    rows,
)
from tests.platform.hub.conftest import HubDatabase, HubHarness, uuid7

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
ROOT_KEY = b64u_encode(bytes(range(32)))
AT = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def remote_body(agent: TestAgent) -> dict[str, Any]:
    return {**agent.pairing_body(), "remote": True}


def create_remote(hub: HubHarness) -> tuple[TestAgent, dict[str, Any]]:
    agent = new_agent(hub)
    response = hub.anon.post("/v1/agent-pairings", json=remote_body(agent))
    assert response.status_code == 200, response.text
    return agent, response.json()


IDENTITIES: dict[uuid.UUID, Identity] = {}
"""The registered identities of the users of `new_user`."""


def remote_handover(agent: TestAgent, user: uuid.UUID, identity: Identity, **options: Any) -> Handover:
    """The browser's part of a remote confirmation: the loopback body plus `handover_ct` (`options` of `handover`)."""
    confirm = handover(agent, user, identity, **options)
    sealed = agent_format.seal_handover(
        agent.identity.kem_public(), str(agent.agent_id), str(user), confirm.secret, ROOT_KEY
    )
    return Handover(secret=confirm.secret, claim=confirm.claim, body={**confirm.body, "handover_ct": sealed})


def new_user(hub: HubHarness) -> uuid.UUID:
    """A user with a passkey session and a registered identity (`IDENTITIES`)."""
    user, identity = uuid7(hub.clock()), Identity.generate()
    hub.keys.session(user)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.user_identity (user_id, kem_pub, sig_pub, recovery_id, created_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (user, identity.kem_public(), identity.sig_public(), b64u_encode(uuid.uuid4().bytes), hub.clock()),
        )
    IDENTITIES[user] = identity
    return user


def confirm_remote(hub: HubHarness, user: uuid.UUID, gen: int = 1) -> tuple[TestAgent, Handover]:
    agent, _answer = create_remote(hub)
    confirm = remote_handover(agent, user, IDENTITIES[user], gen=gen)
    response = gesture_client(hub, user, agent.agent_id).post("/v1/me/agents", json=confirm.body)
    assert response.json() == {"agent_id": str(agent.agent_id)}
    return agent, confirm


def audit_details(hub: HubHarness, action: str) -> list[Any]:
    return [detail for (detail,) in rows(hub, "SELECT detail FROM hub.audit WHERE action = %s ORDER BY seq", action)]


def test_a_remote_pairing_has_a_nonce_and_a_300_second_deadline(hub: HubHarness) -> None:
    agent, answer = create_remote(hub)
    assert set(answer) == {"pairing_id", "expires_at", "remote", "nonce"}
    assert answer["remote"] is True
    nonce = b64u_decode(answer["nonce"], agent_format.NONCE_BYTES)
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["remote"], bytes(row["nonce"]), row["handover_ct"]) == (True, nonce, None)
    assert row["expires_at"] == hub.clock() + timedelta(seconds=store.REMOTE_PAIRING_SECONDS)
    looked_up = hub.anon.get(f"/v1/agent-pairings/{agent.agent_id}").json()
    assert (looked_up["state"], looked_up["remote"], looked_up["nonce"]) == ("open", True, answer["nonce"])
    # the same request again: the stored pairing with the same nonce; the same ID as a loopback pairing: a conflict
    assert hub.anon.post("/v1/agent-pairings", json=remote_body(agent)).json() == answer
    conflict = hub.anon.post("/v1/agent-pairings", json=agent.pairing_body())
    assert (conflict.status_code, conflict.json()) == (409, {"error": "id_conflict"})
    # nonces are drawn per pairing
    assert create_remote(hub)[1]["nonce"] != answer["nonce"]
    hub.clock.advance(store.REMOTE_PAIRING_SECONDS)
    expired = hub.anon.get(f"/v1/agent-pairings/{agent.agent_id}")
    assert (expired.status_code, expired.json()) == (404, {"error": "pairing_unknown"})
    begin = hub.anon.post("/v1/passkeys/login/begin", json={"pairing_id": str(agent.agent_id)})
    assert (begin.status_code, begin.json()) == (404, {"error": "pairing_unknown"})


def test_a_loopback_pairing_and_an_old_agent_are_answered_as_before(hub: HubHarness) -> None:
    agent = new_agent(hub)
    answer = hub.anon.post("/v1/agent-pairings", json=agent.pairing_body()).json()
    assert set(answer) == {"pairing_id", "expires_at"}
    looked_up = hub.anon.get(f"/v1/agent-pairings/{agent.agent_id}").json()
    assert set(looked_up) == {"pairing_id", "pub", "state", "expires_at"}
    explicit = new_agent(hub)
    body = {**explicit.pairing_body(), "remote": False}
    assert set(hub.anon.post("/v1/agent-pairings", json=body).json()) == {"pairing_id", "expires_at"}
    row = agent_row(hub, explicit.agent_id)
    assert row is not None
    assert (row["remote"], row["nonce"]) == (False, None)
    assert row["expires_at"] == hub.clock() + timedelta(seconds=store.PAIRING_SECONDS)
    wrong = hub.anon.post(
        "/v1/agent-pairings", json={**new_agent(hub).pairing_body(), "remote": "yes"}, extensions={"contract": False}
    )
    assert (wrong.status_code, wrong.json()["error"]) == (400, "request_invalid")


def test_the_confirmation_requires_the_handover_for_remote_and_refuses_it_for_loopback(hub: HubHarness) -> None:
    user = new_user(hub)
    identity = IDENTITIES[user]
    remote, _answer = create_remote(hub)
    without = handover(remote, user, identity).body
    response = gesture_client(hub, user, remote.agent_id).post("/v1/me/agents", json=without)
    assert (response.status_code, response.json()) == (400, {"error": "handover_required"})
    loopback = new_agent(hub)
    assert hub.anon.post("/v1/agent-pairings", json=loopback.pairing_body()).status_code == 200
    body = remote_handover(loopback, user, identity).body
    response = gesture_client(hub, user, loopback.agent_id).post("/v1/me/agents", json=body)
    assert (response.status_code, response.json()) == (400, {"error": "handover_unexpected"})
    short = {**remote_handover(remote, user, identity).body, "handover_ct": b64u_encode(bytes(40))}
    response = gesture_client(hub, user, remote.agent_id).post("/v1/me/agents", json=short)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")
    for agent in (remote, loopback):
        row = agent_row(hub, agent.agent_id)
        assert row is not None
        assert row["state"] == "open"
    # a remote pairing past its 300 s cannot be confirmed any more
    hub.clock.advance(store.REMOTE_PAIRING_SECONDS)
    body = remote_handover(remote, user, identity).body
    response = gesture_client(hub, user, remote.agent_id).post("/v1/me/agents", json=body)
    assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})


def test_the_remote_confirmation_notifies_audits_and_gives_600_seconds(
    hub: HubHarness, published: MemoryPublisher
) -> None:
    user = new_user(hub)
    agent, _answer = create_remote(hub)
    hub.clock.advance(200)
    confirm = remote_handover(agent, user, IDENTITIES[user])
    session = gesture_client(hub, user, agent.agent_id)
    assert session.post("/v1/me/agents", json=confirm.body).json() == {"agent_id": str(agent.agent_id)}
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert row["state"] == "confirmed"
    assert bytes(row["handover_ct"]) == b64u_decode(confirm.body["handover_ct"])
    assert row["expires_at"] == hub.clock() + timedelta(seconds=store.REMOTE_CLAIM_SECONDS)
    ref = f"agent_paired_remote::{agent.agent_id}"
    assert [item.data["ref"] for item in published.items() if item.frame == "notification"] == [ref]
    listed = session.get("/v1/notifications", params={"account": "1"}).json()["notifications"]
    assert [(item["kind"], item["ref"], item["org_id"]) for item in listed] == [("agent.account_change", ref, None)]
    assert audit_details(hub, "agent.confirm") == [{"agent": str(agent.agent_id), "remote": True}]
    # an identical repeat is answered alike and notifies nobody again; another handover is a conflict
    published.clear()
    assert session.post("/v1/me/agents", json=confirm.body).json() == {"agent_id": str(agent.agent_id)}
    assert published.items() == []
    other = {**confirm.body, "handover_ct": remote_handover(agent, user, IDENTITIES[user]).body["handover_ct"]}
    response = session.post("/v1/me/agents", json=other)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    repeat_without = {key: value for key, value in confirm.body.items() if key != "handover_ct"}
    response = session.post("/v1/me/agents", json=repeat_without)
    assert (response.status_code, response.json()) == (400, {"error": "handover_required"})


def test_the_agent_fetches_the_handover_checks_the_return_code_and_claims(hub: HubHarness) -> None:
    user = new_user(hub)
    agent, _answer = create_remote(hub)
    client = agent.client(hub)
    # while open the agent's key is not known yet
    assert client.get("/v1/agent/handover").json() == {"error": "key_unknown"}
    identity = IDENTITIES[user]
    confirm = remote_handover(agent, user, identity)
    assert gesture_client(hub, user, agent.agent_id).post("/v1/me/agents", json=confirm.body).status_code == 200
    hub.clock.advance(store.REMOTE_CLAIM_SECONDS - 1)
    fetched = client.get("/v1/agent/handover").json()
    assert fetched == {
        "agent_id": str(agent.agent_id),
        "user_id": str(user),
        "handover_ct": confirm.body["handover_ct"],
    }
    secret, root_key = agent_format.open_handover(
        agent.identity.kem, str(agent.agent_id), str(user), fetched["handover_ct"]
    )
    assert (secret, root_key) == (confirm.secret, ROOT_KEY)
    shown = agent_format.return_code(confirm.secret, str(agent.agent_id), str(user))
    assert agent_format.return_code_matches(secret, str(agent.agent_id), str(user), agent_format.format_code(shown))
    claim = agent_format.claim(secret, str(agent.agent_id), str(user))
    claimed = client.post("/v1/agent/claim", json={"claim": b64u_encode(claim)}).json()
    assert claimed["agent_id"] == str(agent.agent_id)
    opened = agent_format.open_user(agent.identity.kem, secret, str(agent.agent_id), str(user), claimed["user_ct"])
    assert opened == {"cert": confirm.body["cert"], **identity.public()}
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["handover_ct"]) == ("active", None)
    assert audit_details(hub, "agent.claim") == [{"agent": str(agent.agent_id), "remote": True}]
    hub.flush_read_log()
    assert rows(hub, "SELECT agent_id FROM hub.audit WHERE action = 'read.agent.handover'") == [(agent.agent_id,)]
    # after the claim there is no handover any more
    gone = client.get("/v1/agent/handover")
    assert (gone.status_code, gone.json()) == (404, {"error": "pairing_unknown"})
    listed = hub.client(hub.keys.session(user)).get("/v1/me/agents").json()["agents"]
    assert [(item["agent_id"], item["state"], item["remote"]) for item in listed] == [
        (str(agent.agent_id), "active", True)
    ]


def test_the_handover_route_is_only_for_confirmed_remote_rows(hub: HubHarness) -> None:
    user = new_user(hub)
    loopback = new_agent(hub)
    assert hub.anon.post("/v1/agent-pairings", json=loopback.pairing_body()).status_code == 200
    confirm = handover(loopback, user, IDENTITIES[user])
    assert gesture_client(hub, user, loopback.agent_id).post("/v1/me/agents", json=confirm.body).status_code == 200
    response = loopback.client(hub).get("/v1/agent/handover")
    assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})
    assert loopback.client(hub).post("/v1/agent/claim", json=confirm.claim_body).status_code == 200
    response = loopback.client(hub).get("/v1/agent/handover")
    assert (response.status_code, response.json()) == (404, {"error": "pairing_unknown"})
    listed = hub.client(hub.keys.session(user)).get("/v1/me/agents").json()["agents"]
    assert [item["remote"] for item in listed] == [False]
    assert audit_details(hub, "agent.claim") == [{"agent": str(loopback.agent_id)}]
    assert audit_details(hub, "agent.confirm") == [{"agent": str(loopback.agent_id)}]


def test_the_claim_deadline_ends_the_handover_and_the_job_clears_it(hub: HubHarness) -> None:
    user = new_user(hub)
    agent, confirm = confirm_remote(hub, user)
    other, _confirm = confirm_remote(hub, new_user(hub))
    hub.clock.advance(store.REMOTE_CLAIM_SECONDS)
    response = agent.client(hub).get("/v1/agent/handover")
    assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})
    response = agent.client(hub).post("/v1/agent/claim", json=confirm.claim_body)
    assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})
    # the route refuses a row past its deadline even before the job ran
    with hub.pool.connection() as connection, pytest.raises(HubError) as refused:
        store.handover(connection, agent.agent_id, hub.clock())
    assert refused.value.code == "pairing_unknown"
    # the job clears both handovers and revokes both unclaimed own-identity agents (their generations stay counted)
    assert agent_jobs.purge(hub.app.state.hub) == 4
    for found in (agent, other):
        row = agent_row(hub, found.agent_id)
        assert row is not None
        assert (row["state"], row["handover_ct"], row["revoked_reason"]) == ("revoked", None, "unclaimed")
    listed = hub.client(hub.keys.session(user)).get("/v1/me/agents").json()["agents"]
    assert [(item["state"], item["remote"], item["revoked_reason"]) for item in listed] == [
        ("revoked", True, "unclaimed")
    ]
    assert agent_jobs.purge(hub.app.state.hub) == 0


def test_revoking_a_confirmed_remote_agent_clears_the_handover(hub: HubHarness) -> None:
    user = new_user(hub)
    agent, _confirm = confirm_remote(hub, user)
    session = hub.client(hub.keys.session(user))
    assert session.delete(f"/v1/me/agents/{agent.agent_id}").json() == {}
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert (row["state"], row["handover_ct"], row["bundle_ct"]) == ("revoked", None, None)
    assert agent.client(hub).get("/v1/agent/handover").json() == {"error": "key_revoked"}
    # a new remote pairing replaces a confirmed remote one and clears its handover
    first, _confirm = confirm_remote(hub, user, gen=2)
    second, _confirm = confirm_remote(hub, user, gen=3)
    row = agent_row(hub, first.agent_id)
    assert row is not None
    assert (row["state"], row["handover_ct"]) == ("revoked", None)
    replaced = agent_row(hub, second.agent_id)
    assert replaced is not None
    assert replaced["replaces"] == first.agent_id


def test_the_remote_notice_and_the_audit_detail_rules() -> None:
    assert "agent_paired_remote" in notify.ACTIONS
    agent_id = "0190f0f0-0000-7000-8000-000000000001"
    assert detail_violation("agent.confirm", {"agent": agent_id, "remote": True}) is None
    assert detail_violation("agent.claim", {"agent": agent_id}) is None
    assert detail_violation("agent.confirm", {"agent": agent_id, "remote": "yes"}) == "remote"
    assert detail_violation("agent.confirm", {"agent": agent_id, "remote": False}) == "remote"
    assert (
        store.remote_json(
            store.Pairing(
                id=uuid.UUID(agent_id), kem_pub=bytes(32), sig_pub=bytes(32), state="open", expires_at=AT, remote=True
            )
        )
        == {}
    )


def test_0017_applies_on_the_schema_of_0016_with_data(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql") if path.name < "0017")
    for name in names:
        shutil.copy(MIGRATIONS / name, previous / name)
    assert run_migrate(hub_db.migrate_env(), directory=previous) == (f"applied {len(names)}", 0)
    user, agent_id = uuid.uuid4(), uuid.uuid4()
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (user, AT))
        writer.execute(
            "INSERT INTO hub.agent (id, user_id, kem_pub, sig_pub, state, bundle_ct, label_ct, claim_hash, created_at,"
            " expires_at, confirmed_at, claimed_at) VALUES (%s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)",
            (agent_id, user, bytes(32), b"\x01" * 32, bytes(64), bytes(64), bytes(32), AT, AT.replace(hour=11), AT, AT),
        )
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        found = writer.execute("SELECT state, remote, nonce, handover_ct FROM hub.agent").fetchall()
        assert found == [("active", False, None, None)]
        insert = (
            "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, created_at, expires_at, remote, nonce)"
            " VALUES (gen_random_uuid(), %s, %s, 'open', %s, %s, %s, %s)"
        )
        writer.execute(insert, (bytes(32), b"\x02" * 32, AT, AT.replace(hour=11), True, secrets.token_bytes(16)))
        for remote, nonce in ((True, None), (False, bytes(16)), (True, bytes(8))):
            with pytest.raises(psycopg.errors.CheckViolation):
                writer.execute(insert, (bytes(32), b"\x03" * 32, AT, AT.replace(hour=11), remote, nonce))
        # a handover only on a confirmed remote row
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute("UPDATE hub.agent SET handover_ct = %s WHERE id = %s", (bytes(64), agent_id))
        writer.execute("DELETE FROM hub.agent")
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {len(load_migrations())}", 0)
