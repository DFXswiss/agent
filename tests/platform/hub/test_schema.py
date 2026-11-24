"""U-HUB-schema (R-0.2, R-2.10, R-5.3, R-11.9, R-12.2, R-14.7): the hub schema equals 02-data-model.md section 2.

The expectation table below is written from the specification, not from the migration: every table of section 2.1-
2.9 with its exact column names and types in order. An extra column (for example one that would hold confidential
plaintext), a missing one, a project or pull-request table fails the test. Further checks: append-only triggers on
every [AO] table, rights of section 1.4, constraints and indexes the specification names.
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
import pytest

from tests.platform.hub.conftest import HubDatabase

U = "uuid"
T = "text"
TS = "timestamp with time zone"
B = "bytea"
BOOL = "boolean"
INT = "integer"
SI = "smallint"
BI = "bigint"
J = "jsonb"
R = "real"
A = "ARRAY"

EXPECTED: dict[str, list[tuple[str, str]]] = {
    "schema_version": [("version", INT), ("name", T), ("checksum", T), ("applied_at", TS)],
    # 2.1
    "principal": [("id", U), ("kind", T), ("created_at", TS), ("disabled_at", TS)],
    "user_identity": [
        ("user_id", U),
        ("kem_pub", B),
        ("sig_pub", B),
        ("recovery_id", T),
        ("created_at", TS),
        ("id_sig", B),
        ("id_invite_seq", INT),
    ],
    "user_settings": [("user_id", U), ("model_access", T), ("model_id", T), ("locale", T), ("updated_at", TS)],
    "identity_bundle": [("user_id", U), ("slot", T), ("ct", B), ("updated_at", TS)],
    "passkey": [
        ("credential_id", B),
        ("user_id", U),
        ("public_key", B),
        ("sign_count", BI),
        ("transports", A),
        ("prf_seen", BOOL),
        ("created_at", TS),
        ("last_used_at", TS),
        ("removed_at", TS),
    ],
    "ceremony": [
        ("id", U),
        ("kind", T),
        ("challenge", B),
        ("user_id", U),
        ("invite_id", T),
        ("hub_value", B),
        ("created_at", TS),
        ("expires_at", TS),
        ("used_at", TS),
        ("pairing_id", U),  # KOR-24, migration 0010
    ],
    "browser_session": [
        ("key_id", T),
        ("user_id", U),
        ("credential_id", B),
        ("public_key", B),
        ("statement", T),
        ("created_at", TS),
        ("expires_at", TS),
        ("ended_at", TS),
        ("pairing_id", U),  # KOR-24, migration 0010
    ],
    # delegated agent access (KOR-24, ADR-056, migration 0010)
    "agent": [
        ("id", U),
        ("user_id", U),
        ("kem_pub", B),
        ("sig_pub", B),
        ("state", T),
        ("bundle_ct", B),
        ("label_ct", B),
        ("claim_hash", B),
        ("claim_misses", INT),
        ("source", T),
        ("source_net", T),
        ("created_at", TS),
        ("expires_at", TS),
        ("confirmed_at", TS),
        ("claimed_at", TS),
        ("last_used_at", TS),
        ("revoked_at", TS),
        ("replaces", U),
    ],
    "request_nonce": [("key_id", T), ("nonce", T), ("seen_at", TS)],
    "device": [
        ("id", U),
        ("kind", T),
        ("kem_pub", B),
        ("sig_pub", B),
        ("owner_org", U),
        ("state", T),
        ("enrolled_at", TS),
        ("certified_seq", INT),
        ("agent_version", T),
        ("os", T),
        ("arch", T),
        ("capacity", J),
        ("updated_at", TS),
        ("features", A),
    ],
    "device_presence": [("device_id", U), ("hub_instance", T), ("connected_at", TS), ("last_heartbeat_at", TS)],
    "device_enrollment": [("invite_id", T), ("device_id", U), ("request", J), ("created_at", TS), ("certified_at", TS)],
    "invite": [
        ("id", T),
        ("scope", T),
        ("kind", T),
        ("org_id", U),
        ("log_seq", INT),
        ("created_by", U),
        ("created_at", TS),
        ("expires_at", TS),
        ("registered_user", U),
        ("registered_at", TS),
        ("used_at", TS),
        ("used_by", U),
        ("revoked_at", TS),
        ("package_ct", B),
        ("pk_inv", B),  # migration 0007 (KOR-01, ADR-043)
    ],
    # 2.2
    "org": [
        ("id", U),
        ("private", BOOL),
        ("platform_access", BOOL),
        ("default_wg", U),
        ("created_by", U),
        ("created_at", TS),
        ("log_head_seq", INT),
        ("log_head_hash", T),
        ("rotation_pending", J),
    ],
    "membership_entry": [
        ("org_id", U),
        ("seq", INT),
        ("entry_text", T),
        ("entry_hash", T),
        ("type", T),
        ("by_id", U),
        ("subject_id", U),
        ("received_at", TS),
    ],
    "platform_entry": [
        ("seq", INT),
        ("entry_text", T),
        ("entry_hash", T),
        ("type", T),
        ("subject_id", U),
        ("received_at", TS),
    ],
    "org_member": [
        ("org_id", U),
        ("principal_id", U),
        ("kind", T),
        ("role", T),
        ("joined_seq", INT),
        ("removed_seq", INT),
        ("consented", BOOL),
    ],
    "wg": [("id", U), ("org_id", U), ("is_default", BOOL), ("created_seq", INT), ("archived_seq", INT)],
    "wg_access": [
        ("wg_id", U),
        ("user_id", U),
        ("access", T),
        ("granted_seq", INT),
        ("revoked_seq", INT),
        ("inherited", BOOL),
    ],
    "wg_device": [("wg_id", U), ("device_id", U), ("assigned_seq", INT), ("unassigned_seq", INT)],
    "platform_role": [("user_id", U), ("role", T), ("granted_seq", INT), ("revoked_seq", INT)],
    "scope_epoch": [("scope", T), ("scope_id", U), ("epoch", INT), ("created_at", TS), ("created_seq", INT)],
    "key_wrap": [
        ("scope", T),
        ("scope_id", U),
        ("epoch", INT),
        ("recipient_id", U),
        ("wrap", B),
        ("wrapped_by", U),
        ("sig", B),
        ("created_at", TS),
    ],
    # 2.3
    "scope_head": [("scope_id", U), ("scope", T), ("seq", BI)],
    "event": [
        ("scope_id", U),
        ("scope_seq", BI),
        ("id", U),
        ("scope", T),
        ("org_id", U),
        ("wg_id", U),
        ("task_id", U),
        ("session_id", U),
        ("author_id", U),
        ("author_kind", T),
        ("kind", T),
        ("ref", T),
        ("epoch", INT),
        ("ts", TS),
        ("lh", INT),
        ("header_text", T),
        ("ct", B),
        ("sig", B),
        ("size", INT),
        ("origin_device_id", U),
        ("origin_seq", BI),
        ("received_at", TS),
    ],
    "device_stream": [("device_id", U), ("last_origin_seq", BI)],
    # receipts of the device outbox (ADR-030, migration 0002)
    "outbox_receipt": [
        ("device_id", U),
        ("origin_seq", BI),
        ("item_kind", T),
        ("item_sha256", B),
        ("status", T),
        ("error", T),
        ("received_at", TS),
    ],
    # 2.4
    "task": [
        ("id", U),
        ("org_id", U),
        ("wg_id", U),
        ("owner_id", U),
        ("state", T),
        ("state_reason", T),
        ("created_at", TS),
        ("state_at", TS),
        ("last_activity_at", TS),
        ("closed_at", TS),
    ],
    "task_transition": [("task_id", U), ("seq", INT), ("state", T), ("reason", T), ("at", TS), ("by_id", U)],
    "session": [
        ("id", U),
        ("task_id", U),
        ("org_id", U),
        ("wg_id", U),
        ("owner_id", U),
        ("device_id", U),
        ("vm_id", T),
        ("parent_session_id", U),
        ("origin_operation_id", U),
        ("depth", INT),
        ("state", T),
        ("state_reason", T),
        ("model_access", T),
        ("model_id", T),
        ("agent_kind", T),
        ("agent_version", T),
        ("estimate_ms", INT),
        ("created_at", TS),
        ("ready_at", TS),
        ("first_working_at", TS),
        ("ended_at", TS),
    ],
    "session_transition": [
        ("session_id", U),
        ("seq", INT),
        ("state", T),
        ("reason", T),
        ("at", TS),
        ("received_at", TS),
        ("reported_by", U),
    ],
    "vm": [
        ("id", T),
        ("device_id", U),
        ("wg_id", U),
        ("state", T),
        ("golden_version", T),
        ("cpus", INT),
        ("memory_mib", INT),
        ("disk_gib", INT),
        ("created_at", TS),
        ("state_at", TS),
    ],
    # 2.5
    "operation": [
        ("id", U),
        ("org_id", U),
        ("wg_id", U),
        ("task_id", U),
        ("session_id", U),
        ("device_id", U),
        ("actor_id", U),
        ("type", T),
        ("state", T),
        ("error_code", T),
        ("retryable", BOOL),
        ("dedupe_epoch", INT),
        ("dedupe_tag", T),
        ("duplicate_of", U),
        ("attempt", INT),
        ("params_event_id", U),
        ("result_event_id", U),
        ("created_at", TS),
        ("decided_at", TS),
        ("started_at", TS),
        ("finished_at", TS),
    ],
    "operation_transition": [
        ("operation_id", U),
        ("seq", INT),
        ("state", T),
        ("error_code", T),
        ("at", TS),
        ("by_id", U),
    ],
    "dedupe_slot": [("wg_id", U), ("epoch", INT), ("tag", T), ("operation_id", U), ("expires_at", TS)],
    # 2.6
    "whiteboard_entry": [
        ("id", U),
        ("org_id", U),
        ("level", T),
        ("wg_id", U),
        ("task_id", U),
        ("author_id", U),
        ("created_event_id", U),
        ("latest_event_id", U),
        ("created_at", TS),
        ("updated_at", TS),
        ("deleted", BOOL),
        ("notify", BOOL),
    ],
    "session_ping": [
        ("id", U),
        ("session_id", U),
        ("kind", T),
        ("level", T),
        ("ref", T),
        ("created_at", TS),
        ("delivered_at", TS),
    ],
    "notification": [
        ("id", U),
        ("recipient_id", U),
        ("kind", T),
        ("org_id", U),
        ("wg_id", U),
        ("task_id", U),
        ("ref", T),
        ("created_at", TS),
        ("read_at", TS),
    ],
    # 2.7
    "credential": [
        ("id", U),
        ("owner_id", U),
        ("kind", T),
        ("version", INT),
        ("epoch", INT),
        ("header_text", T),
        ("ct", B),
        ("sig", B),
        ("updated_at", TS),
    ],
    "device_request": [
        ("id", U),
        ("user_id", U),
        ("device_id", U),
        ("kind", T),
        ("state", T),
        ("envelope", T),
        ("created_at", TS),
        ("expires_at", TS),
    ],
    # credential-change notices for devices (HUB-13, ADR-035, migration 0003)
    "credential_notice": [("device_id", U), ("owner_id", U), ("kind", T), ("changed_at", TS)],
    "device_response": [("request_id", U), ("seq", INT), ("envelope", T), ("created_at", TS)],
    # 2.8
    "metric_sample": [
        ("entity_kind", T),
        ("entity_id", T),
        ("ts", TS),
        ("device_id", U),
        ("cpu_ratio", R),
        ("mem_used_bytes", BI),
        ("mem_limit_bytes", BI),
        ("disk_used_bytes", BI),
        ("disk_limit_bytes", BI),
        ("net_rx_bytes", BI),
        ("net_tx_bytes", BI),
        ("uptime_seconds", BI),
        ("reachable", BOOL),
        ("state", T),
        ("free_vm_slots", INT),
        ("free_mem_bytes", BI),
        # when the hub stored the sample (HUB-16, migration 0005)
        ("received_at", TS),
        # the session of a work VM sample (KOR-29, migration 0012)
        ("session_id", U),
    ],
    # what the rollup job has seen (HUB-16, migration 0005)
    "metric_rollup_cursor": [("id", SI), ("received_through", TS)],
    "metric_rollup": [
        ("entity_kind", T),
        ("entity_id", T),
        ("ts", TS),
        ("device_id", U),
        ("samples", INT),
        ("cpu_ratio_avg", R),
        ("cpu_ratio_max", R),
        ("mem_used_bytes_avg", BI),
        ("mem_used_bytes_max", BI),
        ("mem_limit_bytes_avg", BI),
        ("mem_limit_bytes_max", BI),
        ("disk_used_bytes_avg", BI),
        ("disk_used_bytes_max", BI),
        ("disk_limit_bytes_avg", BI),
        ("disk_limit_bytes_max", BI),
        ("free_vm_slots_avg", R),
        ("free_vm_slots_max", INT),
        ("free_mem_bytes_avg", BI),
        ("free_mem_bytes_max", BI),
        ("net_rx_bytes", BI),
        ("net_tx_bytes", BI),
        ("uptime_seconds", BI),
        ("reachable", BOOL),
        ("state", T),
    ],
    "usage_counter": [
        ("session_id", U),
        ("requests", BI),
        ("input_tokens", BI),
        ("output_tokens", BI),
        ("cache_read_tokens", BI),
        ("cache_write_tokens", BI),
        ("thinking_tokens", BI),
        ("cost_micro_usd", BI),
        ("cost_basis", T),
        ("context_used_tokens", BI),
        ("context_window_tokens", BI),
        ("last_provider", T),
        ("updated_at", TS),
    ],
    "quota_snapshot": [
        ("owner_id", U),
        ("window", T),
        ("used_percent", R),
        ("resets_at", TS),
        ("severity", T),
        ("fetched_at", TS),
        ("device_id", U),
    ],
    # platform spend per org and UTC day (HUB-14, migration 0006)
    "usage_daily": [("org_id", U), ("day", "date"), ("device_id", U), ("cost_micro_usd", BI)],
    # usage charges and prepaid credit (KOR-31, migration 0014, 12-billing.md section 3)
    "price_list": [("version", INT), ("valid_from", TS), ("document", J), ("created_at", TS)],
    "fx_rate": [
        ("n", INT),
        ("day", "date"),
        ("chf_per_usd_e8", BI),
        ("source", T),
        ("set_at", TS),
        ("actor", T),
        ("reason", T),
    ],
    "credit_account": [("org_id", U), ("balance_micro_usd", BI), ("booked_micro_usd", BI), ("updated_at", TS)],
    "credit_entry": [
        ("org_id", U),
        ("n", INT),
        ("at", TS),
        ("kind", T),
        ("amount_micro_usd", BI),
        ("balance_micro_usd", BI),
        ("reason", T),
        ("actor", T),
        ("fx_n", INT),
        ("fx_day", "date"),
        ("chf_per_usd_e8", BI),
        ("amount_micro_chf", BI),
    ],
    "charge": [
        ("id", U),
        ("org_id", U),
        ("wg_id", U),
        ("task_id", U),
        ("session_id", U),
        ("device_id", U),
        ("origin_seq", BI),
        ("kind", T),
        ("at", TS),
        ("amount_micro_usd", BI),
        ("price_version", INT),
        ("quantity", J),
        ("fx_n", INT),
        ("fx_day", "date"),
        ("chf_per_usd_e8", BI),
        ("amount_micro_chf", BI),
    ],
    "charge_pending": [("charge_id", U), ("org_id", U), ("amount_micro_usd", BI)],
    "charge_daily": [
        ("org_id", U),
        ("day", "date"),
        ("wg_id", U),
        ("device_id", U),
        ("kind", T),
        ("amount_micro_usd", BI),
        ("quantity", BI),
    ],
    # 2.9
    "audit": [
        ("seq", BI),
        ("at", TS),
        ("actor_id", U),
        ("actor_kind", T),
        ("action", T),
        ("result", T),
        ("org_id", U),
        ("wg_id", U),
        ("task_id", U),
        ("session_id", U),
        ("device_id", U),
        ("target_kind", T),
        ("target_id", T),
        ("detail", J),
        ("event_id", U),
        ("prev_hash", T),
        ("hash", T),
        ("agent_id", U),  # KOR-24, migration 0010
    ],
    "audit_checkpoint": [
        ("hub_key_id", T),
        ("seq", BI),
        ("hash", T),
        ("published_at", TS),
        ("signature", B),
    ],
}

APPEND_ONLY = (
    "membership_entry",
    "platform_entry",
    "event",
    "task_transition",
    "session_transition",
    "operation_transition",
    "audit",
    "audit_checkpoint",
    "price_list",
    "fx_rate",
    "credit_entry",
    "charge",
)

PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "identity_bundle": ("user_id", "slot"),
    "request_nonce": ("key_id", "nonce"),
    "membership_entry": ("org_id", "seq"),
    "org_member": ("org_id", "principal_id"),
    "wg_access": ("wg_id", "user_id"),
    "wg_device": ("wg_id", "device_id"),
    "platform_role": ("user_id", "role"),
    "scope_epoch": ("scope", "scope_id", "epoch"),
    "key_wrap": ("scope", "scope_id", "epoch", "recipient_id"),
    "event": ("scope_id", "scope_seq"),
    "task_transition": ("task_id", "seq"),
    "session_transition": ("session_id", "seq"),
    "operation_transition": ("operation_id", "seq"),
    "dedupe_slot": ("wg_id", "epoch", "tag"),
    "device_response": ("request_id", "seq"),
    "credential_notice": ("device_id", "owner_id", "kind"),
    "metric_sample": ("entity_kind", "entity_id", "ts"),
    "metric_rollup": ("entity_kind", "entity_id", "ts"),
    "quota_snapshot": ("owner_id", "window"),
    "usage_daily": ("org_id", "day", "device_id"),
    "credit_entry": ("org_id", "n"),
    "charge_daily": ("org_id", "day", "wg_id", "device_id", "kind"),
    "outbox_receipt": ("device_id", "origin_seq"),
    "audit_checkpoint": ("hub_key_id", "seq"),
}

# Words that would name confidential plaintext (00-overview.md section 3) in a column; 04-crypto.md section 7 lists
# what the hub may see. `entry_text`/`header_text` hold the canonical signed structures, `statement` the session
# certificate statement, `name` only the migration name.
PLAINTEXT_WORDS = re.compile(
    r"(^|_)(title|description|prompt|answer|content|message|email|mail|url|token|password|secret|repo|branch|"
    r"body|plaintext|whiteboard_text|comment|code|params|result|filename|path)($|_)"
)
PLAINTEXT_ALLOWED = {
    ("operation", "params_event_id"),
    ("operation", "result_event_id"),
    ("operation", "error_code"),
    ("operation_transition", "error_code"),
    ("audit", "result"),  # closed list ok/denied/rejected/duplicate/failed
}


def _columns(connection: psycopg.Connection[Any]) -> dict[str, list[tuple[str, str]]]:
    rows = connection.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'hub' ORDER BY table_name, ordinal_position"
    ).fetchall()
    result: dict[str, list[tuple[str, str]]] = {}
    for table, column, data_type in rows:
        result.setdefault(table, []).append((column, data_type))
    return result


def test_tables_and_columns_equal_the_specification(owner: psycopg.Connection[Any]) -> None:
    actual = _columns(owner)
    assert sorted(actual) == sorted(EXPECTED)
    for table, columns in EXPECTED.items():
        assert actual[table] == columns, table


def test_no_project_level_and_no_pull_request_table(owner: psycopg.Connection[Any]) -> None:
    names = owner.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'hub'"
        " UNION ALL SELECT column_name FROM information_schema.columns WHERE table_schema = 'hub'"
    ).fetchall()
    for (name,) in names:
        assert "project" not in name
        assert not re.search(r"(^|_)(pr|pull|pulls|pull_request)(_|$)", name), name


def test_no_column_names_confidential_plaintext(owner: psycopg.Connection[Any]) -> None:
    for table, columns in _columns(owner).items():
        for column, _type in columns:
            if (table, column) in PLAINTEXT_ALLOWED:
                continue
            assert PLAINTEXT_WORDS.search(column) is None, (table, column)


def test_only_the_hub_schema_and_the_trigger_function_exist(owner: psycopg.Connection[Any]) -> None:
    schemas = owner.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname NOT IN"
        " ('information_schema', 'public')"
    ).fetchall()
    assert schemas == [("hub",)]
    functions = owner.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'hub'"
    ).fetchall()
    assert functions == [("refuse_change",)]


@pytest.mark.parametrize(("table", "key"), sorted(PRIMARY_KEYS.items()))
def test_composite_primary_keys(owner: psycopg.Connection[Any], table: str, key: tuple[str, ...]) -> None:
    rows = owner.execute(
        "SELECT a.attname FROM pg_index i"
        " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)"
        " WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)",
        (f"hub.{table}",),
    ).fetchall()
    assert tuple(row[0] for row in rows) == key


def test_every_other_table_has_a_single_column_primary_key(owner: psycopg.Connection[Any]) -> None:
    rows = owner.execute(
        "SELECT c.relname, i.indnatts FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'hub' AND i.indisprimary"
    ).fetchall()
    primary = dict(rows)
    assert sorted(primary) == sorted(EXPECTED)
    for table, count in primary.items():
        assert count == len(PRIMARY_KEYS.get(table, ("one",))), table


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("user_identity", "recovery_id"),
        ("membership_entry", "entry_hash"),
        ("platform_entry", "entry_hash"),
        ("event", "id"),
        ("credential", "owner_id, kind"),
        ("audit", "hash"),
        ("invite", "registered_user"),
    ],
)
def test_unique_constraints(owner: psycopg.Connection[Any], table: str, columns: str) -> None:
    rows = owner.execute(
        "SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indrelid = %s::regclass AND indisunique",
        (f"hub.{table}",),
    ).fetchall()
    assert any(f"({columns})" in row[0] for row in rows), rows


@pytest.mark.parametrize(
    ("table", "definition"),
    [
        ("event", "(task_id, scope_seq)"),
        ("event", "(session_id, kind, scope_seq)"),
        ("event", "(kind, ref)"),
        ("event", "(received_at)"),
        ("dedupe_slot", "(wg_id, epoch, tag)"),
        ("wg", "(org_id) WHERE is_default"),
    ],
)
def test_named_indexes(owner: psycopg.Connection[Any], table: str, definition: str) -> None:
    rows = owner.execute(
        "SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indrelid = %s::regclass", (f"hub.{table}",)
    ).fetchall()
    assert any(row[0].endswith(definition) for row in rows), rows


def _check_values(connection: psycopg.Connection[Any], table: str, column: str) -> str:
    rows = connection.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'",
        (f"hub.{table}",),
    ).fetchall()
    found = [row[0] for row in rows if re.search(rf"\b{column}\b", row[0])]
    assert found, (table, column)
    return " ".join(found)


@pytest.mark.parametrize(
    ("table", "column", "values"),
    [
        ("principal", "kind", ["user", "device"]),
        ("user_settings", "model_access", ["auto", "subscription", "api_key", "platform"]),
        ("user_settings", "locale", ["de", "en"]),
        ("ceremony", "kind", ["register", "login", "add_passkey", "recovery", "recovery_rotate"]),
        ("device", "kind", ["platform", "org"]),
        ("device", "state", ["pending_certification", "active", "decertified", "removed"]),
        ("invite", "scope", ["platform", "org"]),
        ("invite", "kind", ["user", "device"]),
        ("org_member", "role", ["admin", "member", "device"]),
        ("wg_access", "access", ["read", "write"]),
        ("scope_epoch", "scope", ["org", "wg", "cred"]),
        ("task", "state", ["scheduling", "starting", "working", "idle", "failed", "closed"]),
        ("session", "model_access", ["subscription", "api_key", "platform"]),
        ("vm", "state", ["pool", "assigning", "running", "stopping", "stopped", "deleted"]),
        (
            "operation",
            "state",
            ["received", "rejected", "accepted", "duplicate", "denied", "executing", "succeeded", "failed", "expired"],
        ),
        ("whiteboard_entry", "level", ["org", "workgroup", "task"]),
        ("session_ping", "kind", ["whiteboard.entry", "subagent.finished", "operation.finished"]),
        (
            "notification",
            "kind",
            [
                "whiteboard.entry",
                "task.idle",
                "task.failed",
                "invite.accepted",
                "rotation.required",
                "agent.account_change",
                "org.admitted",
            ],
        ),
        ("device_request", "kind", ["github.connect", "claude.login", "credential.check", "mailbox.check"]),
        ("device_request", "state", ["pending", "delivered", "answered", "failed", "expired"]),
        ("metric_sample", "entity_kind", ["device", "vm", "session", "workvm"]),
        ("usage_counter", "cost_basis", ["provider", "list_price", "subscription_equivalent"]),
        ("audit", "actor_kind", ["user", "device", "session", "hub", "operator", "anonymous"]),
        ("audit", "result", ["ok", "denied", "rejected", "duplicate", "failed"]),
    ],
)
def test_closed_value_lists(owner: psycopg.Connection[Any], table: str, column: str, values: list[str]) -> None:
    definition = _check_values(owner, table, column)
    listed = re.findall(r"'([^']*)'::text", definition)
    assert set(values) <= set(listed)
    assert set(listed) - set(values) <= {"session.state"}  # no other value is allowed


def test_session_states_equal_the_contract(owner: psycopg.Connection[Any], repo_root: Any) -> None:
    states = json.loads((repo_root / "contracts/enums/session-states.json").read_text())["states"]
    for table in ("session", "session_transition"):
        listed = re.findall(r"'([^']*)'::text", _check_values(owner, table, "state"))
        assert sorted(listed) == sorted(states)


@pytest.mark.parametrize(
    ("table", "column", "default"),
    [
        ("user_settings", "model_access", "'auto'::text"),
        ("user_settings", "locale", "'en'::text"),
        ("session", "agent_kind", "'claude-code'::text"),
    ],
)
def test_defaults(owner: psycopg.Connection[Any], table: str, column: str, default: str) -> None:
    row = owner.execute(
        "SELECT column_default FROM information_schema.columns WHERE table_schema = 'hub' AND table_name = %s"
        " AND column_name = %s",
        (table, column),
    ).fetchone()
    assert row == (default,)


# --- rights (section 1.4) ------------------------------------------------------------------------------------------

PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _privileges(connection: psycopg.Connection[Any], role: str, table: str) -> set[str]:
    return {
        privilege
        for privilege in PRIVILEGES
        if connection.execute("SELECT has_table_privilege(%s, %s, %s)", (role, f"hub.{table}", privilege)).fetchone()
        == (True,)
    }


def test_writer_rights(migrated_db: HubDatabase, owner: psycopg.Connection[Any]) -> None:
    for table in EXPECTED:
        granted = _privileges(owner, migrated_db.writer_role, table)
        if table == "schema_version":
            assert granted == {"SELECT"}
        elif table in APPEND_ONLY:
            assert granted == {"SELECT", "INSERT"}, table
        else:
            assert granted == {"SELECT", "INSERT", "UPDATE", "DELETE"}, table


def test_public_has_no_rights(migrated_db: HubDatabase, owner: psycopg.Connection[Any]) -> None:
    acls = owner.execute(
        "SELECT c.relname, c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'hub' AND c.relkind = 'r'"
    ).fetchall()
    for table, acl in acls:
        assert acl is not None
        assert not re.search(r"[{,]=", acl), table
    schema_acl = owner.execute("SELECT nspacl::text FROM pg_namespace WHERE nspname = 'hub'").fetchone()
    assert schema_acl is not None
    assert not re.search(r"[{,]=", schema_acl[0])
    database_acl = owner.execute("SELECT datacl::text FROM pg_database WHERE datname = current_database()").fetchone()
    assert database_acl is not None
    assert not re.search(r"[{,]=", database_acl[0])
    assert f"{migrated_db.writer_role}=c/" in database_acl[0]
    function_acl = owner.execute("SELECT proacl::text FROM pg_proc WHERE proname = 'refuse_change'").fetchone()
    assert function_acl is not None
    assert not re.search(r"[{,]=", function_acl[0])


def test_a_role_without_grants_cannot_read(migrated_db: HubDatabase) -> None:
    stranger = f"t_hub_stranger_{migrated_db.name[-12:]}"
    with migrated_db.admin() as admin:
        admin.execute(f"CREATE ROLE {stranger} LOGIN PASSWORD 'x'")
        try:
            with pytest.raises(psycopg.OperationalError, match="permission denied"):
                psycopg.connect(
                    host=migrated_db.host, port=migrated_db.port, dbname=migrated_db.name, user=stranger, password="x"
                )
        finally:
            admin.execute(f"DROP ROLE {stranger}")
