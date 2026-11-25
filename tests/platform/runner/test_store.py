"""U-RUN-store and MIG-01 (runner): cluster lifecycle, migrations and the complete schema `runner`.

02-data-model.md sections 4 and 6, ADR-022. Real PostgreSQL throughout: `runner_dsn` is a fresh database of the
test server; the lifecycle test runs a real `initdb`/`pg_ctl` cluster under a test home.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import threading
import uuid
from pathlib import Path

import psycopg
import psycopg.conninfo
import pytest
from psycopg import sql

from agent_cli.pg import PgError
from agent_runner.errors import RunnerError
from agent_runner.identity import Device, generate_keys, load_identity, write_device, write_keys
from agent_runner.store import migrate as migrate_module
from agent_runner.store.cluster import DATABASE, ClusterTools, start_store, stop_store, store_exists
from agent_runner.store.migrate import (
    ADVISORY_LOCK,
    MIGRATIONS_DIR,
    MigrationResult,
    load_migrations,
    migrate,
)
from agent_runner.store.pool import open_pool

TABLES = {
    "scope_key": ["scope", "scope_id", "epoch", "key_ct", "source", "created_at"],
    "log_entry": ["org_id", "seq", "entry_text", "entry_hash"],
    "log_head": ["org_id", "seq", "hash"],
    "platform_log_entry": ["seq", "entry_text", "entry_hash"],
    "outbox": ["origin_seq", "item_kind", "body_ct", "sealed", "created_at", "pushed_at"],
    "sync_state": ["key", "value"],
    "inbox_cursor": ["scope_id", "scope_seq"],
    "golden": ["version", "lima_name", "claude_version", "guest_version", "image_digest", "built_at", "state"],
    "vm": [
        "id",
        "lima_name",
        "golden_version",
        "wg_id",
        "org_id",
        "state",
        "control_token_hash",
        "cpus",
        "memory_mib",
        "created_at",
        "assigned_at",
        "last_session_at",
    ],
    "session": [
        "id",
        "task_id",
        "org_id",
        "wg_id",
        "vm_id",
        "parent_id",
        "depth",
        "uid",
        "state",
        "claude_session_id",
        "model_access",
        "model_id",
        "credential_owner",
        "created_at",
        "ready_at",
        "ended_at",
        "last_prompt_at",
        # 0016 (HOT-14): the acting user of the current turn
        "turn_actor",
        "turn_open",
    ],
    # 0003 (RUN-05) adds task_id, scope_seq, channel and attempts
    # 0026 (KOR-46, ADR-083) adds via_agent
    "prompt_delivery": [
        "event_id",
        "session_id",
        "state",
        "updated_at",
        "task_id",
        "scope_seq",
        "channel",
        "attempts",
        "via_agent",
    ],
    "operation": [
        "id",
        "session_id",
        "task_id",
        "wg_id",
        "type",
        "state",
        "params",
        "result",
        "error_code",
        "dedupe_tag",
        "attempt",
        "created_at",
        "updated_at",
        # 0004 (RUN-07): the pipeline's request, registration, decision, result event and answer
        "org_id",
        "vm_id",
        "pending_id",
        "requested_at",
        "idempotency_key",
        "local_error",
        "checked",
        "registration",
        "duplicate_of",
        "child_session_id",
        "result_event_id",
        "retryable",
        "answer",
        "answered_at",
        # 0015 (KOR-25): held operations
        "held_reason",
        "held_repo",
        "held_until",
        "held_notified_at",
        "held_ended_at",
        "held_credential",
        # 0016 (HOT-14): the acting user stamped at the receipt
        "actor_id",
        # 0019 (KOR-65): the next step of an operation held for repository access
        "held_next",
    ],
    "guest_cursor": ["vm_id", "journal_seq"],
    "credential_cache": ["owner_id", "kind", "version", "record"],
    "workvm": [
        "id",
        "lima_name",
        "slot",
        "scope",
        "task_id",
        "session_id",
        "requested_by",
        "implicit",
        "org_id",
        "wg_id",
        "owner_id",
        "size",
        "cpus",
        "memory_mib",
        "disk_gib",
        "image",
        "state",
        "created_at",
        "last_used_at",
        "stopped_at",
        "deleted_at",
        "delete_reason",
        # 0026 (KOR-46, ADR-083): the person whose turn requested the work VM
        "triggered_by",
    ],
    "workvm_mirror": ["workvm_id", "session_id", "root", "manifest", "updated_at"],
    "workvm_image": ["name", "state", "built_at"],
    "workvm_pool": ["lima_name", "slot", "image", "memory_mib", "state", "created_at", "ready_at"],
    # 0006 (RUN-09): the last response number of a device request
    "device_request": ["id", "kind", "user_id", "state", "updated_at", "last_seq"],
    # 0002 (RUN-04): latest profile events for the harness context of a session; 0009 (KOR-03): keyed per scope
    "profile_event": ["kind", "org_id", "object_id", "event_id", "ts", "author_sig", "sealed", "scope_id"],
    # 0003 (RUN-05): verified prompt events of the device's workgroups
    # 0016 (HOT-14) adds author_id
    "prompt_event": [
        "event_id",
        "org_id",
        "wg_id",
        "task_id",
        "scope_seq",
        "author_sig",
        "sealed",
        "received_at",
        "author_id",
    ],
    # 0004 (RUN-07): latest verified workgroup settings and the result events of original operations
    "workgroup_settings": ["wg_id", "org_id", "event_id", "scope_seq", "author_sig", "sealed"],
    "operation_result": ["operation_id", "event_id", "org_id", "wg_id", "author_sig", "sealed", "received_at"],
    # 0007 (RUN-08): verified events the session reads answer from
    "read_event": [
        "event_id",
        "kind",
        "org_id",
        "scope",
        "scope_id",
        "scope_seq",
        "task_id",
        "ref",
        "author_id",
        "ts",
        "received_at",
        "author_sig",
        "sealed",
    ],
    # 0005 (RUN-15): metric samples waiting for the hub
    "metric_pending": ["id", "taken_at", "samples"],
    # 0008 (RUN-14): content-free session pings handed to guests
    "ping": ["id", "session_id", "kind", "level", "ref", "created_at", "sent_at", "delivered_at"],
    # 0010 (KOR-08, ADR-047): scope queues of the outbox
    "outbox_held": [
        "id",
        "scope",
        "scope_id",
        "item_kind",
        "event_id",
        "body_ct",
        "sealed",
        "min_epoch",
        "created_at",
        "held_at",
    ],
    # 0011 (KOR-23, ADR-052): the operator's token file a session is bound to
    "operator_binding": ["session_id", "file_name", "bound_at"],
    "billing_meter": ["vm_id", "measured_through"],
    "operator_origin": ["session_id", "bound_at"],
    "credit_pause": ["session_id", "org_id", "interrupted", "paused_at", "resumed_at"],
    # 0022 (KOR-50, ADR-081 sections 2-4): used-up and refused accesses, the model pauses of main sessions
    "access_limit": ["access_key", "kind", "until", "readable", "limited_at", "identity"],
    "model_pause": [
        "session_id",
        "kind",
        "interrupted",
        "paused_at",
        "until",
        "dfx_choice",
        "from_access",
        "from_model",
        "suspended",
        "resumed_at",
    ],
    # 0023 (KOR-50, ADR-081 section 7): the sealed DFX key envelopes of an org device, the requests an issuer answered
    "dfx_key": ["org_id", "key_ref", "issuer_device_id", "envelope", "accepted_at"],
    "dfx_key_issued": ["request_id", "org_id", "device_id", "key_ref", "issued_at"],
    # tokens this device minted through the device flow (KOR-40, migration 0014)
    "github_minted": ["owner_id", "token_sha256", "minted_at"],
    # 0017 (KOR-33, ADR-072): context checkpoints, their restore and pull-request links
    "context_session": [
        "session_id",
        "task_id",
        "org_id",
        "wg_id",
        "due_at",
        "archive_sha256",
        "last_at",
        "attempts",
        "next_at",
        "restored_object_id",
        "restore_audit",
        "restore_started",
        "restore_failed",
    ],
    "context_object": [
        "object_id",
        "task_id",
        "session_id",
        "org_id",
        "wg_id",
        "source",
        "state",
        "manifest",
        "sig",
        "bytes",
        "uploaded",
        "attempts",
        "next_at",
        "created_at",
        "committed_at",
        "kind",
    ],
    "test_run": [
        "run_id",
        "operation_id",
        "session_id",
        "task_id",
        "org_id",
        "wg_id",
        "workvm_id",
        "kind",
        "command",
        "cwd",
        "size",
        "timeout_seconds",
        "digest",
        "state",
        "started_at",
        "ended_at",
        "result",
    ],
    # 0028 (KOR-44, ADR-085): platform browser sessions
    "browser_session": [
        "browser_id",
        "operation_id",
        "session_id",
        "task_id",
        "org_id",
        "wg_id",
        "workvm_id",
        "state",
        "seq",
        "frame_seq",
        "idle_seconds",
        "url",
        "ready_ms",
        "implicit",
        "opened_at",
        "last_call_at",
        "closed_at",
        "close_reason",
    ],
    "context_pr_link": ["operation_id", "state", "attempts", "next_at", "updated_at"],
    # the last audited list of OPERATOR_GITHUB_USERS, keyed hashes only (KOR-47, migration 0022)
    "operator_github_user": ["user_hmac"],
    # the preparation of a merge commit and the workspace digests (KOR-48, migration 0027_merge_prep)
    "merge_prep": [
        "id",
        "session_id",
        "repo",
        "number",
        "branch",
        "head_sha",
        "base_sha",
        "merge_base_sha",
        "changed",
        "conflicts",
        "sides",
        "markers",
        "state",
        "commit_sha",
        "created_at",
    ],
    "workspace_state": ["session_id", "dir", "digest", "commit_sha", "branch", "updated_at"],
    # GitHub identities (KOR-47, migration 0017)
    "github_identity_cache": ["id", "owner_id", "owner_kind", "is_primary", "version", "record"],
    "github_selection": ["session_id", "task_id", "github_identity", "task_github_identity"],
    "github_default": ["scope", "scope_id", "identity_id"],
    # 0026 (KOR-46, ADR-083): withdrawals this device carried out and reported
    "prompt_withdrawal": ["event_id", "withdrawn_at"],
    # 0029 (KOR-35, ADR-086): usage deltas of a session merged over 10 s
    "usage_pending": ["session_id", "item", "first_wall"],
    # 0030 (KOR-45, ADR-088 section 1): the instructions of a child session
    "subagent_instruction": [
        "operation_id",
        "child_session_id",
        "parent_session_id",
        "actor_id",
        "created_at",
        "delivered_at",
        "accepted_at",
        "ended_at",
    ],
    "schema_version": ["version", "name", "checksum", "applied_at"],
}
OUTBOX_KINDS = [
    "event",
    "session_transition",
    "vm_state",
    "operation_transition",
    "usage",
    "quota",
    "metrics",
    "audit",
    "wrap",
]


def uid() -> str:
    return str(uuid.uuid4())


def copy_migrations(target: Path) -> Path:
    target.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(path, target / path.name)
    return target


# --- migrations -------------------------------------------------------------------------------------------------


def test_migration_files() -> None:
    migrations = load_migrations()
    # later packages add numbered migrations (ADR-027 section 5); 0001 is the complete schema of RUN-01
    assert (migrations[0].version, migrations[0].name) == (1, "runner")
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    data = (MIGRATIONS_DIR / "0001_runner.sql").read_bytes()
    assert migrations[0].checksum == hashlib.sha256(data).hexdigest()
    assert ADVISORY_LOCK == 742180200


def test_mig01_applies_on_empty_schema_then_unchanged(runner_dsn: str) -> None:
    count = len(load_migrations())
    first = migrate(runner_dsn)
    assert first == MigrationResult("applied", count=count)
    assert first.line == f"applied {count}"
    second = migrate(runner_dsn)
    assert second == MigrationResult("unchanged", count=count)
    assert second.line == f"unchanged {count}"
    with psycopg.connect(runner_dsn) as conn:
        rows = conn.execute("SELECT version, name, checksum FROM runner.schema_version").fetchall()
    assert rows == [(m.version, m.name, m.checksum) for m in load_migrations()]


def test_mig01_every_table_and_column(runner_dsn: str) -> None:
    migrate(runner_dsn)
    with psycopg.connect(runner_dsn) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'runner' "
            "ORDER BY table_name, ordinal_position"
        ).fetchall()
    found: dict[str, list[str]] = {}
    for table, column in rows:
        found.setdefault(table, []).append(column)
    assert found == TABLES


def test_mig01_constraints_with_fixtures(runner_dsn: str) -> None:
    migrate(runner_dsn)
    org, wg, wg2, session, task = uid(), uid(), uid(), uid(), uid()
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runner.scope_key (scope, scope_id, epoch, key_ct, source) VALUES "
            "('org', %s, 0, '\\x00', 'package'), ('wg', %s, 1, '\\x01', 'wrap'), ('cred', %s, 0, '\\x02', 'wrap')",
            (org, wg, uid()),
        )
        for kind in OUTBOX_KINDS:
            conn.execute(
                "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct) "
                "SELECT COALESCE(MAX(origin_seq), 0) + 1, %s, '\\x00' FROM runner.outbox",
                (kind,),
            )
        conn.execute("INSERT INTO runner.log_entry VALUES (%s, 0, 'entry', 'hash')", (org,))
        conn.execute("INSERT INTO runner.log_head VALUES (%s, 0, 'hash')", (org,))
        conn.execute("INSERT INTO runner.platform_log_entry VALUES (0, 'entry', 'hash')")
        conn.execute("INSERT INTO runner.sync_state VALUES ('pushed_origin_seq', '0')")
        conn.execute("INSERT INTO runner.inbox_cursor VALUES (%s, 5)", (wg,))
        conn.execute(
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state) "
            "VALUES ('g1', 'golden-g1', '2.1.330', '0.1.0', 'sha256:00', 'ready')"
        )
        vm_sql = (
            "INSERT INTO runner.vm (id, lima_name, golden_version, wg_id, org_id, state, control_token_hash, cpus,"
            " memory_mib) VALUES (%s, %s, 'g1', %s, %s, %s, 'h', 4, 8192)"
        )
        conn.execute(vm_sql, ("vm-0000000a", "arp-0000000a", wg, org, "running"))
        conn.execute(vm_sql, ("vm-0000000b", "arp-0000000b", None, None, "pool"))
        conn.execute(vm_sql, ("vm-0000000c", "arp-0000000c", None, None, "pool"))
        conn.execute(vm_sql, ("vm-0000000d", "arp-0000000d", wg2, org, "deleted"))
        conn.execute(vm_sql, ("vm-0000000e", "arp-0000000e", wg2, org, "deleted"))
        conn.execute(vm_sql, ("vm-0000000f", "arp-0000000f", wg2, org, "assigning"))
        conn.execute(
            "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, state) "
            "VALUES (%s, %s, %s, %s, 'vm-0000000a', 'requested')",
            (session, task, org, wg),
        )
        conn.execute(
            "INSERT INTO runner.prompt_delivery (event_id, session_id, state) VALUES (%s, %s, 'pending')",
            (uid(), session),
        )
        conn.execute(
            "INSERT INTO runner.operation (id, session_id, task_id, wg_id, type, state, params) "
            "VALUES (%s, %s, %s, %s, 'task.title', 'received', '{}')",
            (uid(), session, task, wg),
        )
        conn.execute("INSERT INTO runner.guest_cursor VALUES ('vm-0000000a', 0)")
        conn.execute("INSERT INTO runner.credential_cache VALUES (%s, 'mailbox', 1, '{}')", (uid(),))
        conn.execute(
            "INSERT INTO runner.device_request (id, kind, user_id, state) VALUES (%s, 'claude.login', %s, 'open')",
            (uid(), uid()),
        )
        assert conn.execute("SELECT MAX(origin_seq) FROM runner.outbox").fetchone() == (len(OUTBOX_KINDS),)

        refused = [
            "INSERT INTO runner.scope_key (scope, scope_id, epoch, key_ct, source)"
            " VALUES ('x', gen_random_uuid(), 0, '', 'wrap')",
            "INSERT INTO runner.scope_key (scope, scope_id, epoch, key_ct, source)"
            " VALUES ('wg', gen_random_uuid(), 0, '', 'x')",
            "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct) VALUES (100, 'nope', '')",
            "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct) VALUES (0, 'event', '')",
            "INSERT INTO runner.outbox (origin_seq, item_kind, body_ct) VALUES (1, 'event', '')",
            "INSERT INTO runner.golden (version, lima_name, claude_version, guest_version, image_digest, state)"
            " VALUES ('g2', 'golden-g2', 'c', 'g', 'd', 'broken')",
            "INSERT INTO runner.vm (id, lima_name, golden_version, state, control_token_hash, cpus, memory_mib)"
            " VALUES ('vm-1', 'x', 'g1', 'pool', 'h', 4, 8192)",
            "INSERT INTO runner.prompt_delivery (event_id, session_id, state)"
            " VALUES (gen_random_uuid(), gen_random_uuid(), 'lost')",
        ]
        for statement in refused:
            with pytest.raises(psycopg.errors.IntegrityError):
                conn.execute(statement.encode())
        # Several live VMs per workgroup and device (migration 0018, ADR-075 section 4); a Lima name stays unique.
        conn.execute(vm_sql, ("vm-00000010", "arp-00000010", wg, org, "stopped"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(vm_sql, ("vm-00000011", "arp-0000000a", None, None, "pool"))


def test_checksum_mismatch_is_refused(runner_dsn: str, tmp_path: Path) -> None:
    directory = copy_migrations(tmp_path / "m")
    assert migrate(runner_dsn, directory).status == "applied"
    path = directory / "0001_runner.sql"
    path.write_text(path.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(RunnerError) as caught:
        migrate(runner_dsn, directory)
    assert (caught.value.code, caught.value.name) == ("migration_checksum_mismatch", "0001_runner")


def test_unknown_applied_version_is_refused(runner_dsn: str, tmp_path: Path) -> None:
    directory = copy_migrations(tmp_path / "m")
    following = len(load_migrations()) + 1
    (directory / f"{following:04d}_more.sql").write_text("CREATE TABLE runner.more (id int);\n", encoding="utf-8")
    assert migrate(runner_dsn, directory) == MigrationResult("applied", count=following)
    with pytest.raises(RunnerError) as caught:
        migrate(runner_dsn)
    assert (caught.value.code, caught.value.name) == ("migration_unknown", f"{following:04d}")


def test_next_migration_applies_on_previous_schema(runner_dsn: str, tmp_path: Path) -> None:
    assert migrate(runner_dsn).status == "applied"
    directory = copy_migrations(tmp_path / "m")
    following = len(load_migrations()) + 1
    more = directory / f"{following:04d}_more.sql"
    more.write_text("ALTER TABLE runner.sync_state ADD COLUMN note text;\n", encoding="utf-8")
    assert migrate(runner_dsn, directory) == MigrationResult("applied", count=1)
    assert migrate(runner_dsn, directory) == MigrationResult("unchanged", count=following)


def test_failing_migration_reports_the_step_only(runner_dsn: str, tmp_path: Path) -> None:
    directory = copy_migrations(tmp_path / "m")
    following = len(load_migrations()) + 1
    step = f"{following:04d}_broken"
    (directory / f"{step}.sql").write_text("CREATE TABLE runner.vm (secret_detail int);\n", encoding="utf-8")
    result = migrate(runner_dsn, directory)
    assert result == MigrationResult("failed", step=step)
    assert result.line == f"failed {step}"
    assert "secret_detail" not in result.line
    with psycopg.connect(runner_dsn) as conn:
        versions = conn.execute("SELECT version FROM runner.schema_version ORDER BY version").fetchall()
    assert versions == [(version,) for version in range(1, following)]


@pytest.mark.parametrize(
    ("files", "bad"),
    [
        (["0001_a.sql", "0003_c.sql"], "0003_c"),
        (["0002_b.sql"], "0002_b"),
        (["0001_a.sql", "0001_b.sql"], "0001_b"),
        (["1_a.sql"], "1_a.sql"),
        (["0001_A.sql"], "0001_A.sql"),
    ],
)
def test_invalid_migration_sets(files: list[str], bad: str, tmp_path: Path) -> None:
    for name in files:
        (tmp_path / name).write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
    with pytest.raises(RunnerError) as caught:
        load_migrations(tmp_path)
    assert (caught.value.code, caught.value.name) == ("migration_invalid", bad)


def test_unreachable_store() -> None:
    with pytest.raises(RunnerError) as caught:
        migrate("host=127.0.0.1 port=1 user=nobody dbname=none connect_timeout=1")
    assert (caught.value.code, caught.value.name) == ("store_unavailable", None)


def test_advisory_lock_timeout(runner_dsn: str) -> None:
    with psycopg.connect(runner_dsn, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK,))
        with pytest.raises(RunnerError) as caught:
            migrate(runner_dsn, lock_timeout_ms=100)
        assert caught.value.code == "migration_locked"
    assert migrate(runner_dsn).status == "applied"


def test_concurrent_migrations_serialise(runner_dsn: str) -> None:
    results: list[MigrationResult] = []
    threads = [threading.Thread(target=lambda: results.append(migrate(runner_dsn))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.status for r in results) == ["applied", "unchanged", "unchanged"]


def test_lock_released_after_failure(runner_dsn: str, tmp_path: Path) -> None:
    directory = copy_migrations(tmp_path / "m")
    path = directory / "0001_runner.sql"
    assert migrate(runner_dsn, directory).status == "applied"
    path.write_text("-- changed\n", encoding="utf-8")
    with pytest.raises(RunnerError):
        migrate(runner_dsn, directory)
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK,)).fetchone() == (True,)


def test_module_dir() -> None:
    assert migrate_module.MIGRATIONS_DIR.name == "migrations"


# --- pool -------------------------------------------------------------------------------------------------------


def test_pool_opens_and_closes(runner_dsn: str) -> None:
    migrate(runner_dsn)

    async def scenario() -> object:
        pool = await open_pool(runner_dsn, min_size=1, max_size=2)
        try:
            async with pool.connection() as conn:
                row = await (await conn.execute("SELECT count(*) FROM runner.schema_version")).fetchone()
        finally:
            await pool.close()
        return row

    assert asyncio.run(scenario()) == (len(load_migrations()),)


def test_pool_unreachable() -> None:
    async def scenario() -> None:
        await open_pool("host=127.0.0.1 port=1 user=nobody dbname=none connect_timeout=1", timeout=0.5)

    with pytest.raises(RunnerError) as caught:
        asyncio.run(scenario())
    assert caught.value.code == "store_unavailable"


# --- cluster ----------------------------------------------------------------------------------------------------


def test_cluster_lifecycle_real(tmp_path: Path) -> None:
    """Real initdb/pg_ctl under a test home: create, migrate, stop, restart; identity survives a store wipe."""
    home = tmp_path / "home"
    write_keys(home, generate_keys())
    write_device(
        home,
        Device(
            device_id=uid(),
            kind="org",
            hub_url="https://hub.example.org",
            hub_key="A" * 43,
            enrolled_at="2026-11-21T12:00:00.000Z",
        ),
    )
    before = load_identity(home)
    pg_dir = home / "pg"
    try:
        dsn = start_store(pg_dir)
        assert f"dbname={DATABASE}" in dsn
        assert (pg_dir / "port").is_file()
        conf = (pg_dir / "data" / "postgresql.conf").read_text(encoding="utf-8")
        assert "listen_addresses = '127.0.0.1'" in conf
        assert migrate(dsn).status == "applied"
        stop_store(pg_dir)
        stop_store(pg_dir)
        dsn_again = start_store(pg_dir)
        assert dsn_again == dsn
        assert migrate(dsn_again).status == "unchanged"
    finally:
        stop_store(pg_dir)
    shutil.rmtree(pg_dir)
    assert load_identity(home) == before
    try:
        dsn = start_store(pg_dir)
        assert migrate(dsn).status == "applied"
    finally:
        stop_store(pg_dir)
    assert load_identity(home) == before
    stop_store(tmp_path / "never-created")


def failing_ensure(_path: Path) -> str:
    raise PgError("server said: password=hunter2")


def failing_stop(_path: Path) -> None:
    raise OSError("disk")


def test_cluster_errors_hide_server_text(tmp_path: Path) -> None:
    with pytest.raises(RunnerError) as caught:
        start_store(tmp_path / "pg", ClusterTools(ensure=failing_ensure))
    assert (caught.value.code, str(caught.value)) == ("store_unavailable", "store_unavailable")
    with pytest.raises(RunnerError) as caught:
        stop_store(tmp_path / "pg", ClusterTools(stop=failing_stop, exists=lambda _p: True))
    assert caught.value.code == "store_unavailable"


def test_create_database_is_idempotent(runner_pg_admin: str) -> None:
    name = "t_run_" + uuid.uuid4().hex[:12]
    tools = ClusterTools(ensure=lambda _p: runner_pg_admin)
    try:
        first = tools.create_database(runner_pg_admin, name)
        assert tools.create_database(runner_pg_admin, name) == first
    finally:
        with psycopg.connect(runner_pg_admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def test_database_errors_after_connect_hide_their_text(runner_pg_admin: str, runner_dsn: str) -> None:
    """A role without CREATE on the database: the server error becomes `store_unavailable`, no text leaks."""
    role = "t_run_role_" + uuid.uuid4().hex[:8]
    with psycopg.connect(runner_pg_admin, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
    try:
        limited = psycopg.conninfo.make_conninfo(runner_dsn, user=role)
        with pytest.raises(RunnerError) as caught:
            migrate(limited)
        assert (caught.value.code, str(caught.value)) == ("store_unavailable", "store_unavailable")
    finally:
        with psycopg.connect(runner_pg_admin, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_create_database_errors_hide_their_text(tmp_path: Path) -> None:
    def refuse(_admin: str, _name: str) -> str:
        raise psycopg.errors.InsufficientPrivilege("permission denied for database secret_name")

    tools = ClusterTools(ensure=lambda _p: "host=127.0.0.1", create_database=refuse)
    with pytest.raises(RunnerError) as caught:
        start_store(tmp_path / "pg", tools)
    assert str(caught.value) == "store_unavailable"


def test_store_exists(tmp_path: Path) -> None:
    assert store_exists(tmp_path / "pg") is False
    (tmp_path / "pg" / "data").mkdir(parents=True)
    (tmp_path / "pg" / "data" / "PG_VERSION").write_text("16", encoding="utf-8")
    assert store_exists(tmp_path / "pg") is True

    def broken(_path: Path) -> bool:
        raise PgError("cannot inspect cluster under /private/path")

    with pytest.raises(RunnerError) as caught:
        store_exists(tmp_path / "pg", ClusterTools(exists=broken))
    assert str(caught.value) == "store_unavailable"
