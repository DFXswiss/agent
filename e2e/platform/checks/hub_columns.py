"""Column check of the hub database against the exhaustive list of what the hub sees (04-crypto.md section 7, E2E-09).

Every column of schema `hub` has one entry below: the row of 04-crypto.md section 7 it belongs to (`CATEGORIES`) and
its content type (`CONTENT`). The check compares the live schema with this map - a column without an entry, an entry
without a column, or a column whose SQL type does not fit its content type is a deviation - and checks the values of
every text-like column: identifiers, states, kinds, codes, hashes and signed JSON documents (strings and object keys)
consist of tokens only (`TOKEN`: no white space, nothing outside ASCII). That rules out free text with spaces or
non-ASCII letters in a column the map calls metadata; a single ASCII word would pass, which is why the canary scan
(`plaintext_scan.py`, whose canaries include a single-word ASCII one per field) is the check for content itself.
Confidential content is only allowed as ciphertext (`ciphertext` category: sealed under keys the hub never holds,
04-crypto.md section 4). Columns that section 7 does not name literally are listed in ADR-046 with their reasons.

`report(columns, bad)` builds the report from the live columns (`COLUMNS_SQL`) and the result of `values_sql(...)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# The rows of 04-crypto.md section 7 ("What the hub sees in plaintext (exhaustive)") plus encrypted content.
CATEGORIES: dict[str, str] = {
    "ids": "IDs of orgs, workgroups, tasks, sessions, operations, devices, users, events, whiteboard entries",
    "logs": "Membership and platform logs (roles, accesses, public keys, invite IDs, device assignments, epochs)",
    "auth": "Passkey credential IDs and public keys, sign counters, session public keys",
    "events": "Event headers (kind, scope, epoch, author, task, session, ref, ts, lh), sizes, receive times",
    "tasks": "Task state, owner, timestamps; session state, device, VM ID, parent session, depth, model access kind, "
    "model ID",
    "operations": "Operation type, state, decision, error code, dedupe tag, timestamps, actor",
    "metrics": "Resource metrics, token counters, cost, context-window percentage, subscription quota percentages",
    "notifications": "Notification records (recipient, kind, target IDs)",
    "audit": "Audit and read-log rows (actor, action, target IDs, result)",
    "credentials": "Credential metadata (owner, kind, version)",
    "settings": "User settings (model access preference, model choice, locale)",
    "ciphertext": "Encrypted content (ADR-003): sealed under scope, credential or recipient keys the hub never holds",
    "agents": "Agents of a user (11-agent-access.md section 2): agent ID, public keys, state, times, claim hash, the "
    "source address of an open pairing, which login confirmed which pairing, which actions were taken by an agent",
    "internal": "Hub bookkeeping without user data (applied schema migrations)",
}

# Content type → SQL types it may have.
CONTENT: dict[str, frozenset[str]] = {
    "uuid": frozenset({"uuid"}),
    "time": frozenset({"timestamp with time zone", "date"}),
    "number": frozenset({"integer", "bigint", "smallint", "real"}),
    "flag": frozenset({"boolean"}),
    "bytes": frozenset({"bytea"}),  # public keys, signatures, hashes, challenges, ciphertext
    "code": frozenset({"text"}),  # one token: an ID, a kind, a state, a code, a hash, a version
    "codes": frozenset({"ARRAY"}),
    "json": frozenset({"jsonb", "text"}),  # a JSON document whose strings are tokens (signed entries, headers)
    "lines": frozenset({"text"}),  # lines of tokens (a signed statement)
}
CHECKED = ("code", "codes", "json", "lines")
TOKEN = r"^[A-Za-z0-9_.:/+=-]*$"

# table: default category | column:content[@category] ...
_MAP = """
audit: audit | seq:number at:time actor_id:uuid actor_kind:code action:code result:code org_id:uuid wg_id:uuid
  task_id:uuid session_id:uuid device_id:uuid target_kind:code target_id:code detail:json event_id:uuid prev_hash:code
  hash:code agent_id:uuid@agents
agent: agents | id:uuid user_id:uuid kem_pub:bytes sig_pub:bytes state:code bundle_ct:bytes@ciphertext
  label_ct:bytes@ciphertext claim_hash:bytes claim_misses:number source:code source_net:code created_at:time
  expires_at:time confirmed_at:time claimed_at:time last_used_at:time revoked_at:time replaces:uuid
audit_checkpoint: audit | hub_key_id:code seq:number hash:code published_at:time signature:bytes
browser_session: auth | key_id:code user_id:uuid@ids credential_id:bytes public_key:bytes statement:lines
  created_at:time expires_at:time ended_at:time pairing_id:uuid@agents
ceremony: auth | id:uuid kind:code challenge:bytes user_id:uuid@ids invite_id:code@logs hub_value:bytes
  created_at:time expires_at:time used_at:time pairing_id:uuid@agents
credential: credentials | id:uuid owner_id:uuid kind:code version:number epoch:number header_text:json
  ct:bytes@ciphertext sig:bytes updated_at:time
credential_notice: credentials | device_id:uuid owner_id:uuid kind:code changed_at:time
dedupe_slot: operations | wg_id:uuid epoch:number tag:code operation_id:uuid expires_at:time
device: logs | id:uuid@ids kind:code kem_pub:bytes sig_pub:bytes owner_org:uuid state:code enrolled_at:time
  certified_seq:number agent_version:code@metrics os:code@metrics arch:code@metrics capacity:json@metrics
  updated_at:time
device_enrollment: logs | invite_id:code device_id:uuid request:json created_at:time certified_at:time
device_presence: metrics | device_id:uuid hub_instance:code connected_at:time last_heartbeat_at:time
device_request: operations | id:uuid user_id:uuid device_id:uuid kind:code state:code envelope:json@ciphertext
  created_at:time expires_at:time
device_response: operations | request_id:uuid seq:number envelope:json@ciphertext created_at:time
device_stream: events | device_id:uuid last_origin_seq:number
event: events | scope_id:uuid scope_seq:number id:uuid scope:code org_id:uuid wg_id:uuid task_id:uuid
  session_id:uuid author_id:uuid author_kind:code kind:code ref:code epoch:number ts:time lh:number header_text:json
  ct:bytes@ciphertext sig:bytes size:number origin_device_id:uuid origin_seq:number received_at:time
identity_bundle: ciphertext | user_id:uuid@ids slot:code@auth ct:bytes updated_at:time@auth
invite: logs | id:code scope:code kind:code org_id:uuid log_seq:number created_by:uuid created_at:time
  expires_at:time registered_user:uuid registered_at:time used_at:time used_by:uuid revoked_at:time
  package_ct:bytes@ciphertext pk_inv:bytes
key_wrap: logs | scope:code scope_id:uuid epoch:number recipient_id:uuid wrap:bytes@ciphertext wrapped_by:uuid
  sig:bytes created_at:time
membership_entry: logs | org_id:uuid seq:number entry_text:json entry_hash:code type:code by_id:uuid
  subject_id:uuid received_at:time
metric_rollup: metrics | entity_kind:code entity_id:code ts:time device_id:uuid samples:number cpu_ratio_avg:number
  cpu_ratio_max:number mem_used_bytes_avg:number mem_used_bytes_max:number mem_limit_bytes_avg:number
  mem_limit_bytes_max:number disk_used_bytes_avg:number disk_used_bytes_max:number disk_limit_bytes_avg:number
  disk_limit_bytes_max:number free_vm_slots_avg:number free_vm_slots_max:number free_mem_bytes_avg:number
  free_mem_bytes_max:number net_rx_bytes:number net_tx_bytes:number uptime_seconds:number reachable:flag state:code
metric_rollup_cursor: metrics | id:number received_through:time
metric_sample: metrics | entity_kind:code entity_id:code ts:time device_id:uuid cpu_ratio:number
  mem_used_bytes:number mem_limit_bytes:number disk_used_bytes:number disk_limit_bytes:number net_rx_bytes:number
  net_tx_bytes:number uptime_seconds:number reachable:flag state:code free_vm_slots:number free_mem_bytes:number
  received_at:time
notification: notifications | id:uuid recipient_id:uuid kind:code org_id:uuid wg_id:uuid task_id:uuid ref:code
  created_at:time read_at:time
operation: operations | id:uuid org_id:uuid wg_id:uuid task_id:uuid session_id:uuid device_id:uuid actor_id:uuid
  type:code state:code error_code:code retryable:flag dedupe_epoch:number dedupe_tag:code duplicate_of:uuid
  attempt:number params_event_id:uuid result_event_id:uuid created_at:time decided_at:time started_at:time
  finished_at:time
operation_transition: operations | operation_id:uuid seq:number state:code error_code:code at:time by_id:uuid
org: logs | id:uuid@ids private:flag platform_access:flag default_wg:uuid created_by:uuid created_at:time
  log_head_seq:number log_head_hash:code rotation_pending:json
org_member: logs | org_id:uuid principal_id:uuid kind:code role:code joined_seq:number removed_seq:number
outbox_receipt: events | device_id:uuid origin_seq:number item_kind:code item_sha256:bytes status:code error:code
  received_at:time
passkey: auth | credential_id:bytes user_id:uuid@ids public_key:bytes sign_count:number transports:codes
  prf_seen:flag created_at:time last_used_at:time removed_at:time
platform_entry: logs | seq:number entry_text:json entry_hash:code type:code subject_id:uuid received_at:time
platform_role: logs | user_id:uuid role:code granted_seq:number revoked_seq:number
principal: ids | id:uuid kind:code created_at:time disabled_at:time
quota_snapshot: metrics | owner_id:uuid window:code used_percent:number resets_at:time severity:code
  fetched_at:time device_id:uuid
request_nonce: auth | key_id:code nonce:code seen_at:time
schema_version: internal | version:number name:code checksum:code applied_at:time
scope_epoch: logs | scope:code scope_id:uuid epoch:number created_at:time created_seq:number
scope_head: events | scope_id:uuid scope:code seq:number
session: tasks | id:uuid task_id:uuid org_id:uuid wg_id:uuid owner_id:uuid device_id:uuid vm_id:code
  parent_session_id:uuid origin_operation_id:uuid depth:number state:code state_reason:code model_access:code
  model_id:code agent_kind:code agent_version:code estimate_ms:number created_at:time ready_at:time
  first_working_at:time ended_at:time
session_ping: notifications | id:uuid session_id:uuid kind:code level:code ref:code created_at:time
  delivered_at:time
session_transition: tasks | session_id:uuid seq:number state:code reason:code at:time received_at:time
  reported_by:uuid
task: tasks | id:uuid org_id:uuid wg_id:uuid owner_id:uuid state:code state_reason:code created_at:time
  state_at:time last_activity_at:time closed_at:time
task_transition: tasks | task_id:uuid seq:number state:code reason:code at:time by_id:uuid
usage_counter: metrics | session_id:uuid requests:number input_tokens:number output_tokens:number
  cache_read_tokens:number cache_write_tokens:number thinking_tokens:number cost_micro_usd:number cost_basis:code
  context_used_tokens:number context_window_tokens:number last_provider:code updated_at:time
usage_daily: metrics | org_id:uuid day:time device_id:uuid cost_micro_usd:number
user_identity: auth | user_id:uuid@ids kem_pub:bytes@logs sig_pub:bytes@logs recovery_id:code created_at:time
user_settings: settings | user_id:uuid model_access:code model_id:code locale:code updated_at:time
vm: tasks | id:code device_id:uuid wg_id:uuid state:code golden_version:code cpus:number@metrics
  memory_mib:number@metrics disk_gib:number@metrics created_at:time state_at:time
wg: logs | id:uuid@ids org_id:uuid is_default:flag created_seq:number archived_seq:number
wg_access: logs | wg_id:uuid user_id:uuid access:code granted_seq:number revoked_seq:number inherited:flag
wg_device: logs | wg_id:uuid device_id:uuid assigned_seq:number unassigned_seq:number
whiteboard_entry: events | id:uuid@ids org_id:uuid level:code wg_id:uuid task_id:uuid author_id:uuid
  created_event_id:uuid latest_event_id:uuid created_at:time updated_at:time deleted:flag notify:flag@notifications
"""


@dataclass(frozen=True)
class Column:
    table: str
    column: str
    category: str
    content: str


def parse(text: str = _MAP) -> dict[tuple[str, str], Column]:
    """The map: `table: category | column:content[@category] …`, continued on indented lines."""
    out: dict[tuple[str, str], Column] = {}
    joined = text.replace("\n  ", " ")
    for line in filter(None, (raw.strip() for raw in joined.splitlines())):
        head, _, body = line.partition("|")
        table, _, default = head.partition(":")
        for item in body.split():
            column, _, rest = item.partition(":")
            content, _, category = rest.partition("@")
            entry = Column(table.strip(), column, category or default.strip(), content)
            if entry.category not in CATEGORIES or entry.content not in CONTENT:
                raise ValueError(f"bad map entry {table.strip()}.{item}")
            if (entry.table, column) in out:
                raise ValueError(f"{entry.table}.{column} mapped twice")
            out[(entry.table, column)] = entry
    return out


MAP = parse()

COLUMNS_SQL = """SELECT table_name AS "table", column_name AS "column", data_type AS type
  FROM information_schema.columns WHERE table_schema = 'hub' ORDER BY table_name, ordinal_position"""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _offending(entry: Column) -> str:
    """SQL condition: the value of `entry` is not made of tokens only."""
    col = _quote(entry.column)
    if entry.content == "code":
        return f"{col} !~ '{TOKEN}'"
    if entry.content == "codes":
        return f"EXISTS (SELECT 1 FROM unnest({col}) AS v(x) WHERE x !~ '{TOKEN}')"
    if entry.content == "lines":
        return f"EXISTS (SELECT 1 FROM regexp_split_to_table({col}, E'\\n') AS v(x) WHERE x !~ '{TOKEN}')"
    strings = f"jsonb_path_query({col}::jsonb, 'strict $.**') AS v(x) WHERE jsonb_typeof(x) = 'string'"
    keys = f"jsonb_path_query({col}::jsonb, 'strict $.** ? (@.type() == \"object\").keyvalue().key') AS k(x)"
    return (
        f"(EXISTS (SELECT 1 FROM {strings} AND x #>> '{{}}' !~ '{TOKEN}') "
        f"OR EXISTS (SELECT 1 FROM {keys} WHERE x #>> '{{}}' !~ '{TOKEN}'))"
    )


def values_sql(columns: Iterable[Column]) -> str | None:
    """One query over every checked column: `table`, `column`, `bad` (rows with a value that is not made of tokens:
    a string or, in JSON, a string or an object key). No value is returned (the report goes into the artefacts).
    None when there is nothing to check."""
    parts = []
    for entry in columns:
        if entry.content not in CHECKED:
            continue
        where = f"{_quote(entry.column)} IS NOT NULL AND {_offending(entry)}"
        source = f"hub.{_quote(entry.table)}"
        parts.append(
            f"SELECT '{entry.table}' AS \"table\", '{entry.column}' AS \"column\", "
            f"(SELECT count(*) FROM {source} WHERE {where})::int AS bad"
        )
    return " UNION ALL ".join(parts) or None


def checked(columns: Iterable[Mapping[str, Any]]) -> list[Column]:
    """The map entries of the live columns whose type fits (the ones `values_sql` may query)."""
    out = []
    for row in columns:
        entry = MAP.get((str(row["table"]), str(row["column"])))
        if entry is not None and row["type"] in CONTENT[entry.content]:
            out.append(entry)
    return out


def report(columns: Iterable[Mapping[str, Any]], bad: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The column report: one row per live column (category with its 04 section 7 text, content type, values
    checked, offending rows) and the problems (deviations)."""
    findings = {(str(row["table"]), str(row["column"])): row for row in bad}
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for live in columns:
        key = (str(live["table"]), str(live["column"]))
        seen.add(key)
        entry = MAP.get(key)
        row: dict[str, Any] = {"table": key[0], "column": key[1], "type": live["type"]}
        if entry is None:
            problems.append(f"{key[0]}.{key[1]}: column not in the map of 04-crypto.md section 7")
            rows.append({**row, "category": None})
            continue
        row.update(category=entry.category, section7=CATEGORIES[entry.category], content=entry.content)
        if live["type"] not in CONTENT[entry.content]:
            problems.append(f"{key[0]}.{key[1]}: type {live['type']} does not hold {entry.content}")
        found = findings.get(key)
        row["values_checked"] = entry.content in CHECKED
        if found is not None and int(found["bad"]):
            row["bad"] = int(found["bad"])
            problems.append(f"{key[0]}.{key[1]}: {found['bad']} value(s) not made of tokens")
        rows.append(row)
    for key in sorted(set(MAP) - seen):
        problems.append(f"{key[0]}.{key[1]}: mapped but not in the schema")
    categories = sorted({str(row["category"]) for row in rows if row["category"]})
    return {"columns": rows, "categories": {name: CATEGORIES[name] for name in categories}, "problems": problems}
