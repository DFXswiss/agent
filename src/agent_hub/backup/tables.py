"""What an org export holds and how a stored value is written (02-data-model.md section 3, DR-01).

Every table of the hub schema has exactly one rule here, or is named in `EXCLUDED` with its reason; a test compares the
rules with the migrated schema, so a later migration that adds a table cannot be forgotten by the export. A rule is the
condition that selects the rows of one org (`%(org)s` and the ID sets of `Selection`) and whether the rows are
append-only (two exports of one hub must agree on such a row; for every other row the newest export wins when several
exports are restored together, `agent_hub.backup.restore`).

The org's own rows are those of the org, its workgroups, tasks, sessions and operations. Rows of principals (users,
devices) belong to every org the principal is a member of, so they appear in each of its exports. The platform log,
the platform roles and invitations, the platform devices, the audit chain and its checkpoints are hub-wide: they are
single hash chains (or derived from one) that can only be verified and continued as a whole, so every export carries
them completely (decision DR-01 1). The community (ADR-069) belongs to all users of the hub and is carried completely
by every export as well.

Values are written by their column type (`encode`/`decode`): `uuid` and text as JSON strings, integers and booleans as
JSON literals, `timestamptz` as `YYYY-MM-DDTHH:MM:SS.ffffffZ` (microseconds: the stored precision), `date` as
`YYYY-MM-DD`, `bytea` as b64u, `jsonb` as its JSON value, `real` as a JSON number, `text[]` as a JSON array of strings.
A column of any other type stops the export (`ValueUnwritable`), so a new type needs a new rule here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_hub.crypto.encoding import b64u_decode, b64u_encode, is_b64u

FORMAT = "agent-hub-export-v1"
SCHEMA = "hub"

ORG_ROWS = "org_id = %(org)s"
WG_ROWS = "wg_id = ANY(%(wgs)s)"
USER_ROWS = "user_id = ANY(%(users)s)"
DEVICE_ROWS = "device_id = ANY(%(devices)s)"
ALL_ROWS = "TRUE"
SCOPE_ROWS = (
    "(scope = 'org' AND scope_id = %(org)s) OR (scope = 'wg' AND scope_id = ANY(%(wgs)s))"
    " OR (scope = 'cred' AND (scope_id = ANY(%(users)s) OR scope_id = %(org)s))"
)


@dataclass(frozen=True)
class TableRule:
    """The rows of `table` that belong to the export of one org."""

    table: str
    where: str
    append_only: bool = False
    seeded: bool = False
    """The migration inserts a row (an empty database holds it; restore replaces it by the exported row)."""
    columns: Mapping[str, str] = field(default_factory=dict)
    """SQL expressions that replace stored columns in the export (a value that must not leave the hub as it is);
    `%(now)s` is the export time."""


AGENT_EXPORT: Mapping[str, str] = {
    "state": "'revoked'",
    "bundle_ct": "NULL::bytea",
    "user_ct": "NULL::bytea",
    "handover_ct": "NULL::bytea",
    "revoked_at": "COALESCE(revoked_at, %(now)s)",
}

RULES: tuple[TableRule, ...] = (
    # Principals first: user_identity, user_settings and device reference principal; the context tables reference
    # task and context_object (the only other foreign keys, KOR-33).
    TableRule("principal", "id = ANY(%(principals)s)"),
    TableRule("user_identity", USER_ROWS),
    TableRule("user_settings", USER_ROWS),
    TableRule("identity_bundle", USER_ROWS),
    TableRule("passkey", USER_ROWS),
    TableRule("browser_session", USER_ROWS),
    # agents of the org's users (ADR-056); open pairings (no user yet) live ten minutes and are not exported. An agent
    # is exported revoked and without its sealed identity (review L3, decisions KOR-24): an export file never opens an
    # access, and a restored hub has no working agent (the user pairs again).
    TableRule("agent", USER_ROWS, columns=AGENT_EXPORT),
    TableRule("request_nonce", ALL_ROWS),
    TableRule("device", "id = ANY(%(devices)s)"),
    TableRule("device_enrollment", DEVICE_ROWS),
    TableRule("invite", "org_id = %(org)s OR scope = 'platform'"),
    TableRule("org", "id = %(org)s"),
    TableRule("membership_entry", ORG_ROWS, append_only=True),
    TableRule("platform_entry", ALL_ROWS, append_only=True),
    TableRule("org_member", ORG_ROWS),
    TableRule("wg", ORG_ROWS),
    TableRule("wg_access", WG_ROWS),
    TableRule("wg_device", WG_ROWS),
    TableRule("org_device", ORG_ROWS),
    TableRule("wg_placement", WG_ROWS),
    TableRule("platform_role", ALL_ROWS),
    TableRule("platform_policy", ALL_ROWS),  # the switches of the platform log (ADR-079)
    # support (ADR-096): the roster with its names is hub-wide like the platform roles; releases belong to the org
    TableRule("support_person", ALL_ROWS),
    TableRule("support_release", ORG_ROWS),
    TableRule("support_access", WG_ROWS),
    TableRule("scope_epoch", SCOPE_ROWS),
    TableRule("key_wrap", SCOPE_ROWS),
    TableRule("scope_head", "scope_id = %(org)s OR scope_id = ANY(%(wgs)s)"),
    TableRule("event", ORG_ROWS, append_only=True),
    # how far the audit job summarised the session's own events of the org's scopes (ADR-063 section 4)
    TableRule("event_audit_cover", "scope_id = %(org)s OR scope_id = ANY(%(wgs)s)"),
    TableRule("device_stream", DEVICE_ROWS),
    TableRule("outbox_receipt", DEVICE_ROWS),
    TableRule("task", ORG_ROWS),
    # the numbering of the org's tasks (ADR-095 section 1): exported rows keep their numbers, so the counter continues
    TableRule("task_counter", ORG_ROWS),
    # the attention records (ADR-076): a projection whose `since` and `seq` keep the history of the occasions, so it is
    # exported rather than derived again (a missing row is derived by the job `attention.backfill`)
    TableRule("task_attention", ORG_ROWS),
    TableRule("task_transition", "task_id = ANY(%(tasks)s)", append_only=True),
    TableRule("session", ORG_ROWS),
    TableRule("session_transition", "session_id = ANY(%(sessions)s)", append_only=True),
    TableRule("vm", "wg_id = ANY(%(wgs)s) OR (wg_id IS NULL AND device_id = ANY(%(devices)s))"),
    TableRule("operation", ORG_ROWS),
    TableRule("operation_transition", "operation_id = ANY(%(operations)s)", append_only=True),
    TableRule("dedupe_slot", WG_ROWS),
    TableRule("whiteboard_entry", ORG_ROWS),
    TableRule("session_ping", "session_id = ANY(%(sessions)s)"),
    # the org's notifications and the account notifications of its users (`agent.account_change`, no org; ADR-056)
    TableRule("notification", "org_id = %(org)s OR (org_id IS NULL AND recipient_id = ANY(%(users)s))"),
    # the records of the org's users and the org's own company records (ADR-078)
    TableRule("credential", "owner_id = ANY(%(users)s) OR owner_id = %(org)s"),
    TableRule("github_default", "(scope = 'org' AND scope_id = %(org)s) OR (scope = 'wg' AND scope_id = ANY(%(wgs)s))"),
    # the own rights profiles of the org and its workgroups (ADR-094)
    TableRule("agent_profile", "org_id = %(org)s"),
    TableRule("credential_notice", "owner_id = ANY(%(users)s) OR owner_id = %(org)s"),
    TableRule("device_request", USER_ROWS),
    TableRule("device_response", "request_id IN (SELECT id FROM hub.device_request WHERE user_id = ANY(%(users)s))"),
    TableRule("metric_sample", DEVICE_ROWS),
    TableRule("metric_rollup", DEVICE_ROWS),
    TableRule("metric_rollup_cursor", ALL_ROWS, seeded=True),
    TableRule("usage_counter", "session_id = ANY(%(sessions)s)"),
    TableRule("quota_snapshot", "owner_id = ANY(%(users)s)"),
    TableRule("quota_unreadable", "owner_id = ANY(%(users)s)"),
    # the newest reading of each non-primary subscription of the org's users (KOR-69, ADR-090)
    TableRule("subscription_quota", "owner_id = ANY(%(users)s)"),
    TableRule("subscription_use", "owner_id = ANY(%(users)s)"),
    TableRule("mailbox_use", "owner_id = ANY(%(users)s)"),  # KOR-74, ADR-099 section 3
    # model access with fallback (KOR-50, ADR-081): the limit of an owner, the snapshots and issuers of the devices,
    # the per-device DFX keys of the org (envelope ciphertext only) and the cost waiting for a key's booking
    TableRule("quota_limit", "owner_id = ANY(%(users)s)"),
    TableRule("operator_quota", DEVICE_ROWS),
    TableRule("dfx_issuer", DEVICE_ROWS),
    TableRule("dfx_key", ORG_ROWS),
    TableRule("dfx_key_cost", ORG_ROWS),
    TableRule("usage_daily", ORG_ROWS),
    TableRule("price_list", ALL_ROWS, append_only=True),
    TableRule("fx_rate", ALL_ROWS, append_only=True),
    TableRule("credit_account", ORG_ROWS),
    TableRule("credit_entry", ORG_ROWS, append_only=True),
    TableRule("charge", ORG_ROWS, append_only=True),
    TableRule("charge_pending", ORG_ROWS),
    TableRule("charge_daily", ORG_ROWS),
    # the consumption per person (KOR-46; the nil UUID is the unattributed consumption)
    TableRule("charge_person_daily", ORG_ROWS),
    # The community is hub-wide (ADR-069): every export carries it completely; its user columns have no foreign key.
    TableRule("community_epoch", ALL_ROWS),
    TableRule("community_wrap", ALL_ROWS),
    TableRule("community_request", ALL_ROWS),
    TableRule("community_reject", ALL_ROWS),
    TableRule("community_member", ALL_ROWS),
    TableRule("community_moderator", ALL_ROWS),
    TableRule("community_message", ALL_ROWS),
    # context checkpoints and pull-request links reference their task (and the chunks their object): after `task`
    TableRule("context_object", ORG_ROWS),
    TableRule("context_chunk", "object_id IN (SELECT id FROM hub.context_object WHERE org_id = %(org)s)"),
    TableRule("pr_link", WG_ROWS),
    # withdrawals of waiting prompts reference their task (KOR-46, ADR-083 section 3)
    TableRule("prompt_withdrawal", "task_id = ANY(%(tasks)s)"),
    TableRule("audit", ALL_ROWS, append_only=True),
    TableRule("audit_checkpoint", ALL_ROWS, append_only=True),
)

EXCLUDED: Mapping[str, str] = {
    "schema_version": "the target is migrated by `agent-hub migrate`; the header names the applied migrations",
    "ceremony": "passkey ceremonies live 300 s and are purged after a day; open ceremonies end with the old hub",
    "device_presence": "runtime state: a device is online again once it reconnects to the restored hub",
    "browser_watch": "runtime state: watch leases end within 30 s and the open streams write them again (KOR-44)",
    "browser_live": "runtime state: the latest live frame of a watched browser, replaced by the next one (KOR-44)",
    "ui_health_daily": "anonymous hub-wide counters of the web clients (ADR-086 section 5): no org's data",
}

RULE_BY_TABLE: Mapping[str, TableRule] = {rule.table: rule for rule in RULES}

_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_BIGINT = 2**63 - 1


class ValueUnwritable(ValueError):
    """A stored value or column type has no form in the export file; the message never holds the value."""


class ValueInvalid(ValueError):
    """A value of an export file breaks the form of its column; the message never holds the value."""


@dataclass(frozen=True)
class Column:
    """One column of a hub table: its name and `information_schema` data type (`ARRAY` for `text[]`)."""

    name: str
    data_type: str


@dataclass(frozen=True)
class Table:
    """A hub table with its columns in their order and its primary key."""

    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def key_of(self, row: Mapping[str, Any]) -> tuple[Any, ...]:
        """The primary-key values of an encoded row (JSON values, hashable)."""
        return tuple(row[name] for name in self.key)


def schema_tables(connection: psycopg.Connection[Any]) -> dict[str, Table]:
    """Every table of the schema `hub` with its columns and primary key, from the catalog."""
    columns: dict[str, list[Column]] = {}
    for table, name, data_type in connection.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (SCHEMA,),
    ):
        columns.setdefault(str(table), []).append(Column(str(name), str(data_type)))
    keys: dict[str, tuple[str, ...]] = {}
    for table, key in connection.execute(
        "SELECT c.relname, array_agg(a.attname::text ORDER BY k.ordinality)"
        " FROM pg_catalog.pg_index i"
        " JOIN pg_catalog.pg_class c ON c.oid = i.indrelid"
        " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
        " CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)"
        " JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum"
        " WHERE n.nspname = %s AND i.indisprimary GROUP BY c.relname",
        (SCHEMA,),
    ):
        keys[str(table)] = tuple(key)
    return {name: Table(name, tuple(found), keys.get(name, ())) for name, found in columns.items()}


def export_tables(connection: psycopg.Connection[Any]) -> list[Table]:
    """The exported tables in the order of `RULES`; ValueUnwritable when the schema and the rules disagree."""
    tables = schema_tables(connection)
    if set(tables) != set(RULE_BY_TABLE) | set(EXCLUDED):
        raise ValueUnwritable("the hub schema and the export rules name different tables")
    return [tables[rule.table] for rule in RULES]


def format_timestamp(moment: datetime) -> str:
    """A `timestamptz` in UTC with microseconds."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _same(value: Any) -> object:
    return value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_texts(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


Encoder = tuple[Callable[[object], bool], Callable[[Any], object]]
_ENCODERS: Mapping[str, Encoder] = {
    "uuid": (lambda value: isinstance(value, UUID), str),
    "text": (lambda value: isinstance(value, str), _same),
    "integer": (_is_int, _same),
    "smallint": (_is_int, _same),
    "bigint": (_is_int, _same),
    "boolean": (lambda value: isinstance(value, bool), _same),
    "timestamp with time zone": (lambda value: isinstance(value, datetime), format_timestamp),
    "date": (
        lambda value: isinstance(value, date) and not isinstance(value, datetime),
        lambda value: value.isoformat(),
    ),
    "bytea": (lambda value: isinstance(value, bytes | memoryview), lambda value: b64u_encode(bytes(value))),
    "jsonb": (lambda _value: True, _same),
    "real": (lambda value: isinstance(value, float) and math.isfinite(value), _same),
    "ARRAY": (_is_texts, _same),
}


def encode(column: Column, value: object) -> object:
    """The JSON value of a stored value (module docstring)."""
    if value is None:
        return None
    found = _ENCODERS.get(column.data_type)
    if found is None or not found[0](value):
        raise ValueUnwritable(f"a value of {column.name} has no form in the export file")
    return found[1](value)


class _Invalid(ValueError):
    """A value breaks the form of its column type (turned into ValueInvalid with the column name)."""


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _Invalid()
    return value


def _matching(pattern: re.Pattern[str], value: object) -> str:
    text = _text(value)
    if pattern.fullmatch(text) is None:
        raise _Invalid()
    return text


def _uuid(value: object) -> UUID:
    return UUID(_matching(_UUID_RE, value))


def _timestamp(value: object) -> datetime:
    return datetime.strptime(_matching(_TIME_RE, value), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _date(value: object) -> date:
    return date.fromisoformat(_matching(_DATE_RE, value))


def _bytea(value: object) -> bytes:
    text = _text(value)
    if not is_b64u(text):
        raise _Invalid()
    return b64u_decode(text)


def _integer(value: object) -> int:
    if not _is_int(value) or abs(cast(int, value)) > MAX_BIGINT:
        raise _Invalid()
    return cast(int, value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _Invalid()
    return value


def _real(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Invalid()
    return float(value)


def _texts(value: object) -> list[str]:
    if not _is_texts(value):
        raise _Invalid()
    return cast(list[str], value)


_DECODERS: Mapping[str, Callable[[object], object]] = {
    "uuid": _uuid,
    "text": _text,
    "integer": _integer,
    "smallint": _integer,
    "bigint": _integer,
    "boolean": _boolean,
    "timestamp with time zone": _timestamp,
    "date": _date,
    "bytea": _bytea,
    "jsonb": Jsonb,
    "real": _real,
    "ARRAY": _texts,
}


def decode(column: Column, value: object) -> object:
    """The database value of a JSON value of an export file; ValueInvalid outside the column's form."""
    if value is None:
        return None
    try:
        return _DECODERS[column.data_type](value)
    except (_Invalid, ValueError):
        raise ValueInvalid(f"a value of {column.name} breaks its form") from None


def encode_row(table: Table, record: Sequence[Any]) -> dict[str, object]:
    """A stored row as the `row` object of an export line (members in column order)."""
    return {column.name: encode(column, value) for column, value in zip(table.columns, record, strict=True)}


def decode_row(table: Table, row: Mapping[str, object]) -> tuple[object, ...]:
    """The insert values of a `row` object, in column order."""
    return tuple(decode(column, row[column.name]) for column in table.columns)
