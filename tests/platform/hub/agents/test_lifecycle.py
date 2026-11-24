"""U-HUB-agents (jobs, limits and the guide, 11-agent-access.md sections 2 and 4, decisions KOR-24): unclaimed pairings
are purged one hour after their deadline, an orphaned registration loses its agents, the claim deadline shrinks to
60 s at the confirmation, pairings are limited per network and serialised, and `/agents.md` serves the guide of the
build."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_hub.agents import jobs as agent_jobs
from agent_hub.agents import store
from agent_hub.auth import jobs as auth_jobs
from agent_hub.config import load_config
from agent_hub.crypto import agent as agent_format
from agent_hub.errors import HubError
from agent_hub.jobs import Job
from agent_hub.registry import JOBS, concat_sequences
from tests.platform.hub.agents.support import (
    agent_row,
    gesture_client,
    handover,
    new_agent,
    rows,
    stored_agent,
    user_with_identity,
)
from tests.platform.hub.auth.authenticator import FakeBrowser, seed_invite
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, hub_environ, start_hub


def test_the_purge_job_is_registered_and_deletes_unclaimed_pairings_after_an_hour(hub: HubHarness) -> None:
    assert [job.name for job in agent_jobs.JOBS] == ["agents.unclaimed_purge"]
    assert "agents.unclaimed_purge" in [job.name for job in concat_sequences(JOBS)]
    assert all(isinstance(job, Job) for job in agent_jobs.JOBS)
    opened = stored_agent(hub, state="open")
    confirmed = stored_agent(hub, state="confirmed")
    active, revoked = stored_agent(hub), stored_agent(hub, state="revoked")
    context = hub.app.state.hub
    hub.clock.advance(600 + 3600)
    assert agent_jobs.purge(context) == 0
    hub.clock.advance(0.001)
    assert agent_jobs.purge(context) == 2
    assert agent_row(hub, opened.agent_id) is None
    assert agent_row(hub, confirmed.agent_id) is None
    assert agent_row(hub, active.agent_id) is not None
    assert agent_row(hub, revoked.agent_id) is not None


def test_an_orphaned_registration_loses_its_agents(browser: FakeBrowser) -> None:
    hub = browser.hub
    account = browser.register(seed_invite(hub))
    user = uuid.UUID(account.user_id)
    agents = [stored_agent(hub, user), stored_agent(hub, user, state="revoked")]
    other = stored_agent(hub)
    hub.clock.advance(24 * 3600)
    assert auth_jobs.release_orphan_registrations(hub.app.state.hub) == 1
    assert [agent_row(hub, agent.agent_id) for agent in agents] == [None, None]
    assert agent_row(hub, other.agent_id) is not None
    assert agents[0].client(hub).get("/v1/agent/me").json() == {"error": "key_unknown"}


def test_the_confirmation_shortens_the_claim_deadline_to_60_seconds(hub: HubHarness) -> None:
    user, identity = user_with_identity(hub)
    agent = new_agent(hub)
    assert hub.anon.post("/v1/agent-pairings", json=agent.pairing_body()).status_code == 200
    hub.clock.advance(30)
    confirm = handover(agent, user, identity)
    assert gesture_client(hub, user, agent.agent_id).post("/v1/me/agents", json=confirm.body).status_code == 200
    row = agent_row(hub, agent.agent_id)
    assert row is not None
    assert row["expires_at"] == hub.clock() + timedelta(seconds=store.CLAIM_SECONDS)
    hub.clock.advance(store.CLAIM_SECONDS)
    response = agent.client(hub).post("/v1/agent/claim", json=confirm.claim_body)
    assert (response.status_code, response.json()) == (401, {"error": "key_unknown"})
    # a confirmation late in the pairing keeps the earlier deadline
    late = new_agent(hub)
    assert hub.anon.post("/v1/agent-pairings", json=late.pairing_body()).status_code == 200
    created = hub.clock()
    hub.clock.advance(570)
    body = handover(late, user, identity, gen=2).body
    assert gesture_client(hub, user, late.agent_id).post("/v1/me/agents", json=body).status_code == 200
    row = agent_row(hub, late.agent_id)
    assert row is not None
    assert row["expires_at"] == created + timedelta(seconds=store.PAIRING_SECONDS)


def create(hub: HubHarness, source: str, connection: Any = None) -> uuid.UUID:
    agent = new_agent(hub)
    body = agent.pairing_body()
    arguments = {
        "kem": body["pub"]["kem"],
        "sig": body["pub"]["sig"],
        "pop": body["pop"],
        "source": source,
        "now": hub.clock(),
    }
    if connection is not None:
        store.create_pairing(connection, agent.agent_id, **arguments)
        return agent.agent_id
    with hub.pool.connection() as fresh, fresh.transaction():
        store.create_pairing(fresh, agent.agent_id, **arguments)
    return agent.agent_id


def refused(hub: HubHarness, source: str) -> bool:
    try:
        create(hub, source)
    except HubError as error:
        return error.code == "rate_limited"
    return False


def test_pairings_are_limited_per_ipv4_network(hub: HubHarness) -> None:
    for _ in range(3):
        create(hub, "198.51.100.1")
    assert refused(hub, "198.51.100.1")  # 3 per address
    for host in range(2, 9):
        create(hub, f"198.51.100.{host}")
    assert refused(hub, "198.51.100.200")  # 10 per /24
    assert rows(hub, "SELECT count(*) FROM hub.agent WHERE source_net::inet <<= '198.51.100.0/24'") == [(10,)]
    create(hub, "203.0.113.1")


def test_pairings_are_limited_per_ipv6_56_and_48(hub: HubHarness) -> None:
    for net in range(10):
        create(hub, f"2001:db8:0:{net:x}::1")  # ten /64 of one /56
    assert refused(hub, "2001:db8:0:a::1")  # 10 per /56
    for block in (0x100, 0x200):
        for net in range(10):
            create(hub, f"2001:db8:0:{block + net:x}::1")
    assert refused(hub, "2001:db8:0:300::1")  # 30 per /48
    create(hub, "2001:db8:1::1")


def test_a_source_that_is_no_address_has_no_network_limits() -> None:
    assert store.source_limits("testclient") == []
    assert store.source_limits("::ffff:198.51.100.7") == [("198.51.100.0/24", 10)]
    assert store.source_limits("2001:db8:1:2::1") == [("2001:db8:1::/56", 10), ("2001:db8:1::/48", 30)]


def test_concurrent_pairings_with_the_same_keys_are_one_conflict(hub: HubHarness) -> None:
    """The lock serialises creations: the second transaction waits, sees the committed keys and gets a conflict."""
    agent = new_agent(hub)
    body = agent.pairing_body()
    other_id = new_agent(hub).agent_id
    results: list[object] = []
    with hub.pool.connection() as first, first.transaction():
        store.create_pairing(
            first,
            agent.agent_id,
            kem=body["pub"]["kem"],
            sig=body["pub"]["sig"],
            pop=body["pop"],
            source="198.51.100.1",
            now=hub.clock(),
        )

        def second() -> None:
            pop = agent_format.sign_pop(agent.identity, str(other_id))
            try:
                with hub.pool.connection() as connection, connection.transaction():
                    store.create_pairing(
                        connection,
                        other_id,
                        kem=body["pub"]["kem"],
                        sig=body["pub"]["sig"],
                        pop=pop,
                        source="198.51.100.2",
                        now=hub.clock(),
                    )
                results.append("created")
            except HubError as error:
                results.append(error.code)

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(0.5)
        assert results == []  # waiting for the lock
    thread.join(10)
    assert results == ["id_conflict"]


def test_a_unique_violation_on_the_keys_is_a_conflict(hub: HubHarness) -> None:
    """Without the lock (another writer), the unique index refuses the keys: `id_conflict`, not a 500."""
    agent = new_agent(hub)
    body = agent.pairing_body()
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, created_at, expires_at)"
            " VALUES (gen_random_uuid(), %s, %s, 'open', %s, %s)",
            (agent.identity.kem_public(), agent.identity.sig_public(), hub.clock(), hub.clock() + timedelta(seconds=1)),
        )
    response = hub.anon.post("/v1/agent-pairings", json=body)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})


# --- /agents.md ------------------------------------------------------------------------------------------------------


@pytest.fixture
def guide_hub(
    migrated_db: HubDatabase, tmp_path: Path, hub_pool: ConnectionPool[psycopg.Connection[Any]], clock: FixedClock
) -> Iterator[HubHarness]:
    (tmp_path / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    config = load_config(hub_environ(migrated_db, AGENT_HUB_STATIC_DIR=str(tmp_path)))
    yield from start_hub(config, hub_pool, clock)


def test_the_guide_is_served_from_the_build(guide_hub: HubHarness) -> None:
    missing = guide_hub.anon.get("/agents.md")
    assert (missing.status_code, missing.json()) == (404, {"error": "not_found"})
    text = "# Guide for AI agents\n\nPair with `agent-platform pair`.\n"
    (guide_hub.config.static_dir / "agents.md").write_text(text, encoding="utf-8")
    response = guide_hub.anon.get("/agents.md")
    assert response.status_code == 200
    assert response.text == text
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert "content-security-policy" in response.headers
    # an agent's signed request gets the same guide
    assert stored_agent(guide_hub).client(guide_hub).get("/agents.md").text == text
