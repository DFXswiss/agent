"""The platform-log vectors of `contracts/vectors/platform-log.json` through the hub API (07-testing.md section 4):
a hub configured with the vector root accepts the valid log entry by entry with the vector heads and projections, and
refuses every negative vector with its code (the client-only `platform_root_mismatch` answers `log_genesis`)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_hub.config import load_config
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.keys import ed25519_private
from agent_hub.errors import HubError
from agent_hub.platform import log as platform_log
from agent_hub.platform.log import HUB_CODES
from tests.platform.contracts.harness import CONTRACTS
from tests.platform.hub.conftest import FixedClock, HubDatabase, HubHarness, SigningKey, hub_environ, start_hub

VECTORS = json.loads((CONTRACTS / "vectors" / "platform-log.json").read_text(encoding="utf-8"))
LOG: list[dict[str, Any]] = VECTORS["logs"]["platform"]


@pytest.fixture
def vector_hub(
    migrated_db: HubDatabase,
    hub_pool: ConnectionPool[psycopg.Connection[Any]],
    clock: FixedClock,
    owner: psycopg.Connection[Any],
) -> Iterator[HubHarness]:
    # the vector devices run a current runner: every device the log certifies has announced `log.admit`, so the user
    # certifications of the vector log are not held back (ADR-060, `peer_outdated`), nor the switches (`log.policy`,
    # ADR-079)
    owner.execute("ALTER TABLE hub.device ALTER COLUMN features SET DEFAULT '{log.admit,log.policy}'")
    config = load_config(hub_environ(migrated_db, AGENT_HUB_PLATFORM_ROOT_KEY=VECTORS["root"]))
    for harness in start_hub(config, hub_pool, clock):
        org = VECTORS["valid"]["platform_access"][0]["org"]
        with hub_pool.connection() as connection:
            connection.execute(
                "INSERT INTO hub.org (id, private, default_wg, created_by, created_at, log_head_seq, log_head_hash)"
                " VALUES (%s, false, gen_random_uuid(), gen_random_uuid(), now(), 0, 'h')",
                (org,),
            )
        yield harness


def root_client(hub: HubHarness) -> Any:
    root = ed25519_private(b64u_decode(VECTORS["identities"]["root"]["sig_sk"], 32))
    key = SigningKey(key_id="platform-root", sign=root.sign, principal=hub.keys.root.principal)
    return hub.client(key)


def post(hub: HubHarness, expected: int, entries: list[dict[str, Any]], **kwargs: Any) -> Any:
    return root_client(hub).post("/v1/platform/log", json={"expected_seq": expected, "entries": entries}, **kwargs)


def test_the_valid_log_is_accepted_with_the_vector_heads_and_projections(vector_hub: HubHarness) -> None:
    for seq, entry in enumerate(LOG):
        response = post(vector_hub, seq, [entry])
        assert response.status_code == 200, response.text
        assert response.json()["head"] == VECTORS["valid"]["heads"][seq]
        projection = VECTORS["valid"]["projections"][seq]
        with vector_hub.pool.connection() as connection:
            roles = connection.execute(
                "SELECT user_id::text, role FROM hub.platform_role WHERE revoked_seq IS NULL ORDER BY 1"
            ).fetchall()
            devices = connection.execute("SELECT id::text, state, certified_seq FROM hub.device ORDER BY 1").fetchall()
            access = connection.execute("SELECT id::text, platform_access FROM hub.org").fetchall()
            policies = connection.execute('SELECT name, "on" FROM hub.platform_policy').fetchall()
        assert [{"user": user, "role": role} for user, role in roles] == projection["roles"]
        status = {"active": "certified", "decertified": "decertified"}
        assert [
            {"device": device, "status": status[state], "certify_seq": certified}
            for device, state, certified in devices
        ] == projection["devices"]
        assert {org: granted for org, granted in access if org in projection["org_access"]} == projection["org_access"]
        assert dict(policies) == projection["policies"]


INVALID = list(VECTORS["invalid"])


@pytest.mark.parametrize("vector", INVALID, ids=[item["name"] for item in INVALID])
def test_every_negative_vector_is_refused_with_its_code(vector_hub: HubHarness, vector: dict[str, Any]) -> None:
    args = vector["args"]
    prefix = LOG[: args["log"]["count"]]
    expected = HUB_CODES.get(vector["error"], vector["error"])
    if args["root"] != VECTORS["root"]:
        # Another pinned root: the hub configured with that key refuses the log of the vector root.
        with vector_hub.pool.connection() as connection, pytest.raises(HubError) as refused:
            platform_log.append(connection, b64u_decode(args["root"], 32), 0, prefix, vector_hub.clock())
        assert refused.value.code == expected
        return
    if vector["op"] == "append_platform_log":
        assert post(vector_hub, 0, prefix).status_code == 200
        response = post(vector_hub, len(prefix), args["append"], extensions={"contract": False})
    elif prefix:
        if len(prefix) > 1:
            assert post(vector_hub, 0, prefix[:-1]).status_code == 200
        batch = [prefix[-1], *args["entries"]]
        response = post(vector_hub, len(prefix) - 1, batch, extensions={"contract": False})
    else:
        response = post(vector_hub, 0, args["entries"], extensions={"contract": False})
    assert response.status_code in (400, 409), response.text
    assert response.json()["error"] == expected
