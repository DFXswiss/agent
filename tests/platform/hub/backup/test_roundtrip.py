"""Export and restore on real PostgreSQL (DR-01, 02-data-model.md section 3, 07-testing.md section 5.6).

The filled hub (`Estate`: a row in every exported table) is exported org by org, restored into an empty migrated
database, compared table by table, exported again (byte-identical files) and served by a hub started on the restored
database: same log head, same scope heads, a new event continues its scope and the audit chain.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent_core.record.connection import parse_connection_string
from agent_hub.audit.query import verify as verify_audit
from agent_hub.backup.export import write_export
from agent_hub.backup.restore import SESSION_STATEMENTS, restore
from agent_hub.backup.tables import EXCLUDED, RULES
from agent_hub.config import HubConfig, load_config
from agent_hub.db.pool import open_pool
from tests.platform.hub.backup.support import Estate, lines, rows_of, table_rows
from tests.platform.hub.conftest import HubDatabase, HubHarness, hub_environ, start_hub
from tests.platform.hub.orgs.support import new_id

Pool = ConnectionPool[psycopg.Connection[Any]]


def restored(target: HubDatabase, files: list[bytes]) -> int:
    """Restore `files` into `target` as its owner; the number of rows."""
    config = load_config(hub_environ(target))
    with target.connect(target.owner_role, autocommit=True) as connection:
        for statement in SESSION_STATEMENTS:
            connection.execute(statement)
        return restore(connection, target.owner_role, files, config)


@pytest.fixture
def exported(estate: Estate) -> list[bytes]:
    """The exports of both orgs of the estate."""
    return [estate.export(), estate.export(estate.other_org)]


@pytest.fixture
def target_hub(estate: Estate, exported: list[bytes], target: HubDatabase) -> Iterator[HubHarness]:
    """A hub on the restored target, with the source's clock and key resolvers (the clients keep their keys)."""
    restored(target, exported)
    config: HubConfig = load_config(hub_environ(target))
    pool: Pool = open_pool(parse_connection_string(target.writer_url), min_size=1, max_size=4)
    try:
        yield from start_hub(config, pool, estate.hub.clock, resolvers=estate.hub.keys.resolvers())
    finally:
        pool.close()


def test_every_exported_table_holds_rows_of_the_estate(estate: Estate) -> None:
    data = estate.export()
    assert {item["table"] for item in lines(data)[1:-1]} == {rule.table for rule in RULES}


def test_restore_of_every_org_reproduces_every_table(
    estate: Estate, migrated_db: HubDatabase, target: HubDatabase
) -> None:
    files = [estate.export(), estate.export(estate.other_org)]
    count = restored(target, files)
    assert count == len({(item["table"], str(item["row"])) for data in files for item in lines(data)[1:-1]})
    for rule in RULES:
        assert table_rows(target, rule.table) == table_rows(migrated_db, rule.table), rule.table
    assert table_rows(migrated_db, "ceremony") != []  # excluded tables are not restored
    for table in sorted(set(EXCLUDED) - {"schema_version"}):
        assert table_rows(target, table) == [], table


def test_a_restored_hub_exports_byte_identical_files(estate: Estate, target_hub: HubHarness) -> None:
    for org in (estate.org, estate.other_org):
        original = estate.export(org)
        out = io.BytesIO()
        with target_hub.pool.connection() as connection:
            connection.autocommit = True
            write_export(connection, UUID(org), target_hub.config.signer(), out, target_hub.clock())
        again = out.getvalue()
        assert again == original


def test_a_restored_hub_serves_the_same_heads_and_continues(
    estate: Estate, exported: list[bytes], target_hub: HubHarness
) -> None:
    scene = estate.scene
    old_log = scene.writer.browser.get(f"/v1/orgs/{estate.org}/log").json()
    writer = target_hub.client(scene.writer.browser.key)
    assert writer.get(f"/v1/orgs/{estate.org}/log").json() == old_log
    old_events = scene.read(scene.writer).json()
    assert writer.get(f"/v1/scopes/{scene.wg}/events").json() == old_events
    head = estate.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg)[0][0]
    with target_hub.pool.connection() as connection:
        restored_audit = connection.execute("SELECT max(seq) FROM hub.audit").fetchone()
    assert restored_audit == (lines(exported[1])[0]["audit_head"]["seq"],)
    response = writer.post("/v1/events", json={"events": [{"event": scene.wg_event(scene.writer, ref=new_id())}]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["scope_seq"] == head + 1
    signer = target_hub.config.signer()
    with target_hub.pool.connection() as connection:
        result = verify_audit(connection, signer.key_id, signer.public_key)
    assert result["ok"] is True
    assert result["head"]["seq"] > restored_audit[0]


def test_the_export_holds_the_checkpoint_and_the_whole_audit_chain(estate: Estate) -> None:
    data = estate.export()
    audit = rows_of(data, "audit")
    assert [row["seq"] for row in audit] == list(range(1, len(audit) + 1))
    assert rows_of(data, "audit_checkpoint")[0]["seq"] <= len(audit)
