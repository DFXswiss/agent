"""U-HUB-agents (data, 11-agent-access.md section 2, decisions KOR-24): org exports carry the agents of the org's
users - revoked and without the sealed identity - and their account notifications, never open pairings or other
users' rows; a restore brings them back that way; migration 0010 applies on the schema of 0009 with data
(notifications, audit rows of before KOR-24 whose hashes stay valid)."""

from __future__ import annotations

import io
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_hub.audit.chain import AuditRow, audit_hash, verify_chain
from agent_hub.backup.export import write_export
from agent_hub.backup.restore import SESSION_STATEMENTS, restore
from agent_hub.config import load_config
from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from tests.platform.hub.agents.support import rows, stored_agent
from tests.platform.hub.backup.support import fresh_database, rows_of
from tests.platform.hub.conftest import HubDatabase, HubHarness, hub_environ, uuid7
from tests.platform.hub.orgs.support import World

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
AT = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
USER = uuid.UUID("0190f0f0-0000-7000-8000-000000000001")
ORG = uuid.UUID("0190f0f0-0000-7000-8000-000000000002")


def notify(hub: HubHarness, user: uuid.UUID, org: uuid.UUID | None) -> uuid.UUID:
    found = uuid7(hub.clock())
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.notification (id, recipient_id, kind, org_id, ref, created_at) VALUES (%s, %s, %s, %s, %s,"
            " %s)",
            (
                found,
                user,
                "task.idle" if org else "agent.account_change",
                org,
                None if org else "agent_revoked::",
                hub.clock(),
            ),
        )
    return found


def test_an_org_export_carries_its_users_agents_revoked_and_their_account_notifications(hub: HubHarness) -> None:
    world = World(hub)
    founder = world.user()
    draft = world.public_org(founder)
    user = uuid.UUID(founder.id)
    active = stored_agent(hub, user)
    revoked = stored_agent(hub, user, state="revoked")
    stored_agent(hub, state="open")
    foreign = stored_agent(hub)
    mine = notify(hub, user, None)
    org_notice = notify(hub, user, uuid.UUID(draft.id))
    assert foreign.user_id is not None
    notify(hub, foreign.user_id, None)
    out = io.BytesIO()
    with hub.pool.connection() as connection:
        connection.autocommit = True
        write_export(connection, uuid.UUID(draft.id), hub.config.signer(), out, hub.clock())
    data = out.getvalue()
    agents = {row["id"]: row for row in rows_of(data, "agent")}
    assert set(agents) == {str(active.agent_id), str(revoked.agent_id)}
    for row in agents.values():
        assert (row["state"], row["bundle_ct"]) == ("revoked", None)
    assert agents[str(active.agent_id)]["revoked_at"] == hub.clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert {row["id"] for row in rows_of(data, "notification")} == {str(mine), str(org_notice)}
    # the stored agents are unchanged by the export
    assert rows(hub, "SELECT state FROM hub.agent WHERE id = %s", active.agent_id) == [("active",)]
    with fresh_database() as target:
        assert run_migrate(target.migrate_env())[1] == 0
        with target.connect(target.owner_role, autocommit=True) as connection:
            for statement in SESSION_STATEMENTS:
                connection.execute(statement)
            restore(connection, target.owner_role, [data], load_config(hub_environ(target)))
        with target.admin() as connection:
            restored = connection.execute("SELECT id, state, bundle_ct FROM hub.agent ORDER BY id").fetchall()
            notices = connection.execute("SELECT id, org_id FROM hub.notification ORDER BY id").fetchall()
    assert {(str(found), state, ct) for found, state, ct in restored} == {
        (agent_id, "revoked", None) for agent_id in agents
    }
    assert {found for found, _org in notices} == {mine, org_notice}


def previous_schema(database: HubDatabase, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    previous.mkdir()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql") if path.name < "0010")
    for name in names:
        shutil.copy(MIGRATIONS / name, previous / name)
    assert run_migrate(database.migrate_env(), directory=previous) == (f"applied {len(names)}", 0)


def test_0010_applies_on_the_schema_of_0009_with_data(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous_schema(hub_db, tmp_path)
    old_row = AuditRow(at=AT, actor_kind="user", actor_id=USER, action="read.me", result="ok")
    digest = audit_hash(1, old_row, None)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute("INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', %s)", (USER, AT))
        writer.execute(
            "INSERT INTO hub.notification (id, recipient_id, kind, org_id, ref, created_at)"
            " VALUES (%s, %s, 'task.idle', %s, NULL, %s)",
            (uuid.UUID("0190f0f0-0000-7000-8000-000000000003"), USER, ORG, AT),
        )
        writer.execute(
            "INSERT INTO hub.audit (seq, at, actor_id, actor_kind, action, result, detail, prev_hash, hash)"
            " VALUES (1, %s, %s, 'user', 'read.me', 'ok', %s, NULL, %s)",
            (AT, USER, Jsonb({}), digest),
        )
        writer.execute(
            "INSERT INTO hub.browser_session (key_id, user_id, credential_id, public_key, statement, created_at,"
            " expires_at) VALUES ('session.' || repeat('a', 32), %s, %s, %s, 'x', %s, %s)",
            (USER, b"c" * 16, b"\x04" + bytes(64), AT, AT.replace(hour=22)),
        )
    assert run_migrate(hub_db.migrate_env()) == (f"applied {len(load_migrations())}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        assert writer.execute("SELECT kind, org_id FROM hub.notification").fetchall() == [("task.idle", ORG)]
        assert writer.execute("SELECT agent_id, hash FROM hub.audit").fetchall() == [(None, digest)]
        assert writer.execute("SELECT pairing_id FROM hub.browser_session").fetchall() == [(None,)]
        assert verify_chain(writer).ok
        # the new rows: an account notification without org, an agent row
        writer.execute(
            "INSERT INTO hub.notification (id, recipient_id, kind, org_id, ref, created_at)"
            " VALUES (gen_random_uuid(), %s, 'agent.account_change', NULL, 'agent_revoked::', %s)",
            (USER, AT),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute(
                "INSERT INTO hub.notification (id, recipient_id, kind, org_id, created_at)"
                " VALUES (gen_random_uuid(), %s, 'task.idle', NULL, %s)",
                (USER, AT),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute(
                "INSERT INTO hub.notification (id, recipient_id, kind, org_id, created_at)"
                " VALUES (gen_random_uuid(), %s, 'agent.account_change', %s, %s)",
                (USER, ORG, AT),
            )
        writer.execute(
            "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, source, source_net, created_at, expires_at)"
            " VALUES (gen_random_uuid(), %s, %s, 'open', '198.51.100.7', '198.51.100.7', %s, %s)",
            (bytes(32), bytes(32), AT, AT.replace(hour=11)),
        )
        with pytest.raises(psycopg.errors.InvalidTextRepresentation):
            writer.execute(
                "INSERT INTO hub.agent (id, kem_pub, sig_pub, state, source_net, created_at, expires_at)"
                " VALUES (gen_random_uuid(), %s, %s, 'open', 'nope', %s, %s)",
                (bytes(32), b"\x01" * 32, AT, AT.replace(hour=11)),
            )
        writer.execute("DELETE FROM hub.agent")
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {len(load_migrations())}", 0)
