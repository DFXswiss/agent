"""`agent-hub restore --in <file> [--in <file> ...]`: restore org exports into an empty hub database (02 section 3).

One run restores one or more files of `agent_hub.backup.export` (the export of every org of a hub, 07-testing.md
section 5.6) in a single transaction, and only into an empty, migrated database. Steps, each refusing with its name
(`failed <step>[:<check>]`, never a value or a database message, pattern of `agent-hub migrate`):

1. `config`: the hub configuration (`AGENT_HUB_*`, as for the hub process) and the owner's connection string
   `AGENT_HUB_OWNER_DATABASE` of the same database (decision DR-01 3: restore inserts into the append-only tables,
   so it runs, like `migrate`, as the owner and never inside the hub process); a file that cannot be read is
   `file:read`;
2. `owner`, `lock`: the login role owns the database and is no superuser; the advisory lock of `migrate` and an
   `ACCESS EXCLUSIVE` lock on every hub table, so neither a migration nor a running hub writes meanwhile;
3. `schema`: the tables of the target are those the export rules know, and the target's applied migrations equal
   those named by every file;
4. `file`: every file is in the writer's form (each line re-serialises to its own bytes; members, order and value
   forms as written; header, rows in table order, trailer), its `sha256` and row count match, its rows are consistent
   with its header (the org's log head, the platform-log and audit heads); no org appears twice;
5. `signature`: the trailer is signed by the configured hub key (`hub_key_id` equal to `AGENT_HUB_HUB_KEY_ID`);
6. `not_empty`: every hub table except `schema_version` and the tables a migration seeds (`TableRule.seeded`, the
   rollup cursor) is empty; the seeded rows are replaced by the exported ones;
7. `conflict`: the rows of all files are merged by primary key - identical rows are one row; for an append-only table
   two different rows with one key refuse the restore; for any other table the row of the newest export
   (`exported_at`) wins (exports of one restore should come from one state of the hub, see decision DR-01 1);
8. `verify`: the merged rows are checked before anything is inserted - the platform log (chain, signatures, P1-P4
   against the configured root key, stored hashes), every membership log (L1-L8 against the founder's registered
   key, stored hashes, the org's head), every event (canonical header, the extracted columns, the author's
   signature E4, `scope_seq` gapless per scope and equal to `scope_head`), the whole audit chain from `seq` 1 and every
   checkpoint (signature by the hub key, hash of its audit row);
9. `insert`: the rows in the order of `agent_hub.backup.tables.RULES` (triggers stay enabled: rows are only
   inserted, append-only tables are never changed);
10. `projections`: every registered rebuilder runs on the restored rows and must not change a table (the projections
   in the files equal the projections of their logs and events, `agent_hub.backup.rebuild.compare`).

The run prints `restored <rows>` and exits 0, or prints `failed <step>` and exits 1 (2 for a configuration error);
nothing is committed unless every step passed. Restore writes no audit row: the restored audit head equals the
exported one, which E2E-33 asserts; the hub's next audit row continues the chain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_core.record.connection import ConnectionString, ConnectionStringError, read_connection_string
from agent_core.signing import b64url_decode
from agent_hub.audit.chain import AuditRow, StoredAudit, verify_rows
from agent_hub.audit.checkpoint import AuditCheckpoint, checkpoint_signature_valid
from agent_hub.backup import rebuild
from agent_hub.backup.export import HEADER_ORDER, TRAILER_ORDER, line, signed_text
from agent_hub.backup.tables import (
    EXCLUDED,
    FORMAT,
    RULE_BY_TABLE,
    RULES,
    Table,
    ValueInvalid,
    decode_row,
    export_tables,
    schema_tables,
)
from agent_hub.config import HubConfig
from agent_hub.crypto import event as event_format
from agent_hub.crypto.encoding import b64u_encode, canonical
from agent_hub.crypto.errors import CryptoError
from agent_hub.db.migrate import MIGRATE_LOCK, OWNER_VARIABLE, WRITER_VARIABLE
from agent_hub.log.entries import entry_hash
from agent_hub.log.platform import PlatformState, verify_platform_log
from agent_hub.log.verify import VerifiedLog, verify_log

Connection = psycopg.Connection[Any]
Row = dict[str, Any]
LOCK_TIMEOUT_MS = 60000
SESSION_STATEMENTS: tuple[LiteralString, ...] = (
    "SET ROLE NONE",
    "SET search_path = pg_catalog, pg_temp",
    "SET TIME ZONE 'UTC'",
)
_OWNER_CHECK = """
SELECT session_user OPERATOR(pg_catalog.=) r.rolname, r.rolsuper, d.datdba OPERATOR(pg_catalog.=) r.oid
FROM pg_catalog.pg_roles r
JOIN pg_catalog.pg_database d ON d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()
WHERE r.rolname OPERATOR(pg_catalog.=) %s
"""
HEAD_KEYS = ("seq", "hash")


class RestoreFailed(Exception):
    """A step refused the restore; `step` is printed, never a value."""

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def _file_invalid(check: str) -> RestoreFailed:
    return RestoreFailed(f"file:{check}")


def _verify_failed(check: str) -> RestoreFailed:
    return RestoreFailed(f"verify:{check}")


# --- reading one file ------------------------------------------------------------------------------------------------


@dataclass
class ExportFile:
    """One parsed export file: header, rows per table (encoded, in file order), the signed trailer."""

    header: Row
    rows: dict[str, list[Row]] = field(default_factory=dict)
    trailer: Row = field(default_factory=dict)


def _refuse_constant(_name: str) -> object:
    raise ValueError("not JSON")


def _object(raw: bytes) -> Row:
    """One line as a JSON object in the writer's form; `file:line` otherwise (also for a member written twice)."""
    twice = False

    def pairs(items: list[tuple[str, Any]]) -> Row:
        nonlocal twice
        if len({name for name, _ in items}) != len(items):
            twice = True
        return dict(items)

    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise _file_invalid("line") from None
    if twice or not isinstance(value, dict) or line(value) != raw + b"\n":
        raise _file_invalid("line")
    return cast(Row, value)


def _head_valid(value: object, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    return (
        isinstance(value, dict)
        and tuple(value) == HEAD_KEYS
        and isinstance(value["seq"], int)
        and not isinstance(value["seq"], bool)
        and isinstance(value["hash"], str)
    )


def _header(value: Row) -> Row:
    if tuple(value) != HEADER_ORDER or value["format"] != FORMAT:
        raise _file_invalid("header")
    if not all(isinstance(value[name], str) for name in ("org_id", "exported_at", "hub_key_id")):
        raise _file_invalid("header")
    schema = value["schema"]
    if not isinstance(schema, list) or not all(isinstance(item, str) for item in schema):
        raise _file_invalid("header")
    if not _head_valid(value["log_head"], nullable=False):
        raise _file_invalid("header")
    if not _head_valid(value["platform_head"], nullable=True) or not _head_valid(value["audit_head"], nullable=True):
        raise _file_invalid("header")
    return value


def parse_file(data: bytes, tables: Mapping[str, Table]) -> ExportFile:
    """Check the form of one file (step `file`) and return it; the signature is checked by `check_signature`."""
    if not data.endswith(b"\n"):
        raise _file_invalid("end")
    lines = data[:-1].split(b"\n")
    if len(lines) < 2:
        raise _file_invalid("end")
    parsed = ExportFile(header=_header(_object(lines[0])))
    trailer = _object(lines[-1])
    if tuple(trailer) != TRAILER_ORDER or trailer["trailer"] != FORMAT:
        raise _file_invalid("trailer")
    order = [rule.table for rule in RULES]
    position = 0
    for raw in lines[1:-1]:
        value = _object(raw)
        if tuple(value) != ("table", "row") or value["table"] not in RULE_BY_TABLE:
            raise _file_invalid("row")
        table = tables[value["table"]]
        row = value["row"]
        if not isinstance(row, dict) or tuple(row) != table.names:
            raise _file_invalid("row")
        index = order.index(table.name)
        if index < position:
            raise _file_invalid("order")
        position = index
        try:
            decode_row(table, row)
        except ValueInvalid:
            raise _file_invalid("value") from None
        parsed.rows.setdefault(table.name, []).append(row)
    body = data[: len(data) - len(lines[-1]) - 1]
    count = sum(len(found) for found in parsed.rows.values())
    if trailer["rows"] != count or trailer["sha256"] != hashlib.sha256(body).hexdigest():
        raise _file_invalid("digest")
    parsed.trailer = trailer
    _check_heads(parsed)
    return parsed


def _last(rows: Sequence[Row], seq: str, head: str) -> Row | None:
    if not rows:
        return None
    last = max(rows, key=lambda row: cast(int, row[seq]))
    return {"seq": last[seq], "hash": last[head]}


def _check_heads(parsed: ExportFile) -> None:
    """The header's heads are those of the file's rows, and the file holds its org."""
    orgs = parsed.rows.get("org", [])
    if [row["id"] for row in orgs] != [parsed.header["org_id"]]:
        raise _file_invalid("org")
    org = orgs[0]
    if {"seq": org["log_head_seq"], "hash": org["log_head_hash"]} != parsed.header["log_head"]:
        raise _file_invalid("heads")
    if _last(parsed.rows.get("platform_entry", []), "seq", "entry_hash") != parsed.header["platform_head"]:
        raise _file_invalid("heads")
    if _last(parsed.rows.get("audit", []), "seq", "hash") != parsed.header["audit_head"]:
        raise _file_invalid("heads")


def check_signature(parsed: ExportFile, hub_key_id: str, public_key: bytes) -> None:
    """Step `signature`: the trailer is signed by the configured hub key."""
    trailer = parsed.trailer
    text = signed_text(parsed.header["hub_key_id"], parsed.header["org_id"], trailer["rows"], trailer["sha256"])
    signature = b64url_decode(trailer["signature"])
    if parsed.header["hub_key_id"] != hub_key_id or signature is None or len(signature) != 64:
        raise RestoreFailed("signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, text)
    except InvalidSignature:
        raise RestoreFailed("signature") from None


def check_signatures(files: Sequence[ExportFile], hub_key_id: str, public_key: bytes) -> None:
    """`check_signature` for every file."""
    for parsed in files:
        check_signature(parsed, hub_key_id, public_key)


# --- merging ---------------------------------------------------------------------------------------------------------


def merge(files: Sequence[ExportFile], tables: Mapping[str, Table]) -> dict[str, list[Row]]:
    """Step `conflict`: the union of the files by primary key, newest export first (module docstring)."""
    orgs = [parsed.header["org_id"] for parsed in files]
    if len(set(orgs)) != len(orgs):
        raise RestoreFailed("file:duplicate_org")
    newest_first = sorted(files, key=lambda parsed: cast(str, parsed.header["exported_at"]), reverse=True)
    merged: dict[str, list[Row]] = {}
    for rule in RULES:
        table = tables[rule.table]
        found: dict[tuple[Any, ...], tuple[Row, str]] = {}
        for parsed in newest_first:
            stamp = cast(str, parsed.header["exported_at"])
            for row in parsed.rows.get(rule.table, []):
                key = table.key_of(row)
                if key not in found:
                    found[key] = (row, stamp)
                elif found[key][0] != row and (rule.append_only or found[key][1] == stamp):
                    raise RestoreFailed("conflict")
        merged[rule.table] = [found[key][0] for key in sorted(found, key=_sort_key)]
    return merged


def _sort_key(key: tuple[Any, ...]) -> tuple[tuple[int, Any], ...]:
    """Primary keys of encoded rows: integers before strings, so mixed NULL-free keys sort stably."""
    return tuple((0, item) if isinstance(item, int) else (1, str(item)) for item in key)


# --- verification ----------------------------------------------------------------------------------------------------


def verify_platform(rows: Sequence[Row], root_key: bytes) -> PlatformState | None:
    """The platform log: stored hashes, `seq` of each row, the whole chain against the root key."""
    if not rows:
        return None
    entries = [json.loads(row["entry_text"]) for row in rows]
    for row, entry in zip(rows, entries, strict=True):
        body = entry.get("body") if isinstance(entry, dict) else None
        if not isinstance(body, dict) or body.get("seq") != row["seq"] or row["entry_hash"] != entry_hash(entry):
            raise _verify_failed("platform_log")
    try:
        return verify_platform_log(entries, b64u_encode(root_key)).state
    except CryptoError:
        raise _verify_failed("platform_log") from None


def verify_org(
    org: Row, rows: Sequence[Row], founders: Mapping[str, str], platform: PlatformState | None
) -> VerifiedLog:
    """One membership log: stored hashes and `seq`, L1-L8 against the founder's registered key, the org's head."""
    entries = [json.loads(row["entry_text"]) for row in rows]
    for row, entry in zip(rows, entries, strict=True):
        body = entry.get("body") if isinstance(entry, dict) else None
        if not isinstance(body, dict) or body.get("seq") != row["seq"] or row["entry_hash"] != entry_hash(entry):
            raise _verify_failed("membership_log")
    founder = founders.get(org["created_by"])
    if founder is None:
        raise _verify_failed("founder")
    try:
        log = verify_log(entries, founder, platform=platform)
    except CryptoError:
        raise _verify_failed("membership_log") from None
    if (log.head.seq, log.head.hash) != (org["log_head_seq"], org["log_head_hash"]):
        raise _verify_failed("org_head")
    return log


def _expected_columns(header: Mapping[str, Any], ct: bytes) -> dict[str, object]:
    scope = header["scope"]
    return {
        "id": header["id"],
        "scope": scope,
        "scope_id": header["scope_id"],
        "org_id": header["org"],
        "wg_id": header["scope_id"] if scope == "wg" else header["wg"] or None,
        "task_id": header["task"] or None,
        "session_id": header["session"] or None,
        "author_id": header["author"],
        "kind": header["kind"],
        "ref": header["ref"],
        "epoch": header["epoch"],
        "lh": header["lh"],
        "size": len(ct),
    }


def verify_event(row: Row, logs: Mapping[str, VerifiedLog]) -> None:
    """One event: canonical header, extracted columns, the author's signature at the org's log."""
    try:
        header = json.loads(row["header_text"])
        if not isinstance(header, dict) or canonical(header).decode("ascii") != row["header_text"]:
            raise _verify_failed("event_header")
        log = logs[str(header.get("org"))]
        author = log.state.principals[str(header.get("author"))]
        ct = cast(bytes, b64url_decode(row["ct"]))
        event = {"header": header, "ct": row["ct"], "sig": row["sig"]}
        event_format.verify_event(event, cast(bytes, b64url_decode(author.sig)))
    except (CryptoError, KeyError, ValueError, TypeError):
        raise _verify_failed("event") from None
    expected = _expected_columns(header, ct)
    if any(row[name] != value for name, value in expected.items()) or header["lh"] > log.head.seq:
        raise _verify_failed("event_columns")
    if row["author_kind"] != author.kind or row["ts"] != header["ts"][:-1] + "000Z":
        raise _verify_failed("event_columns")


def verify_events(rows: Sequence[Row], heads: Sequence[Row], logs: Mapping[str, VerifiedLog]) -> None:
    """Every event, and `scope_seq` gapless from 1 per scope with the scope head at the last one."""
    counts: dict[str, int] = {}
    for row in rows:
        verify_event(row, logs)
        scope_id = row["scope_id"]
        counts[scope_id] = counts.get(scope_id, 0) + 1
        if row["scope_seq"] != counts[scope_id]:
            raise _verify_failed("scope_seq")
    stored = {row["scope_id"]: row["seq"] for row in heads}
    if any(stored.get(scope_id) != count for scope_id, count in counts.items()):
        raise _verify_failed("scope_head")
    if any(counts.get(scope_id, 0) != seq for scope_id, seq in stored.items()):
        raise _verify_failed("scope_head")


def _uuid(value: object) -> UUID | None:
    return None if value is None else UUID(cast(str, value))


def _time(value: object) -> datetime:
    return datetime.strptime(cast(str, value), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def stored_audit(row: Row) -> StoredAudit:
    """An encoded audit row as the chain verifier reads it."""
    audit = AuditRow(
        at=_time(row["at"]),
        actor_kind=row["actor_kind"],
        action=row["action"],
        result=row["result"],
        actor_id=_uuid(row["actor_id"]),
        org_id=_uuid(row["org_id"]),
        wg_id=_uuid(row["wg_id"]),
        task_id=_uuid(row["task_id"]),
        session_id=_uuid(row["session_id"]),
        device_id=_uuid(row["device_id"]),
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        detail=row["detail"],
        event_id=_uuid(row["event_id"]),
        agent_id=_uuid(row["agent_id"]),
        on_behalf_of=_uuid(row["on_behalf_of"]),
    )
    return StoredAudit(seq=row["seq"], row=audit, prev_hash=row["prev_hash"], hash=row["hash"])


def verify_audit(rows: Sequence[Row], checkpoints: Sequence[Row], hub_key_id: str, public_key: bytes) -> None:
    """The whole audit chain from `seq` 1 and every checkpoint against the hub key and its audit row."""
    if not verify_rows([stored_audit(row) for row in rows], 1, None).ok:
        raise _verify_failed("audit_chain")
    hashes = {row["seq"]: row["hash"] for row in rows}
    for row in checkpoints:
        checkpoint = AuditCheckpoint(
            hub_key_id=row["hub_key_id"],
            seq=row["seq"],
            hash=row["hash"],
            published_at=_time(row["published_at"]),
            signature=cast(bytes, b64url_decode(row["signature"])),
        )
        if (
            row["hub_key_id"] != hub_key_id
            or hashes.get(row["seq"]) != row["hash"]
            or not checkpoint_signature_valid(public_key, checkpoint)
        ):
            raise _verify_failed("checkpoint")


def verify(merged: Mapping[str, Sequence[Row]], config: HubConfig) -> None:
    """Step `verify` over the merged rows (module docstring)."""
    platform = verify_platform(merged["platform_entry"], config.platform_root_key)
    founders = {row["user_id"]: row["sig_pub"] for row in merged["user_identity"]}
    entries: dict[str, list[Row]] = {}
    for row in merged["membership_entry"]:
        entries.setdefault(row["org_id"], []).append(row)
    logs = {org["id"]: verify_org(org, entries.get(org["id"], []), founders, platform) for org in merged["org"]}
    if set(entries) - set(logs):
        raise _verify_failed("membership_log")
    verify_events(merged["event"], merged["scope_head"], logs)
    signer = config.signer()
    verify_audit(merged["audit"], merged["audit_checkpoint"], signer.key_id, signer.public_key)


# --- the database ----------------------------------------------------------------------------------------------------


def owner_string(environ: Mapping[str, str]) -> ConnectionString:
    """The owner's connection string: another role of the database of `AGENT_HUB_DATABASE` (step `config`)."""
    try:
        owner = read_connection_string(environ, OWNER_VARIABLE)
        writer = read_connection_string(environ, WRITER_VARIABLE)
    except ConnectionStringError:
        raise RestoreFailed("config") from None
    same_database = (owner.host, owner.port, owner.database) == (writer.host, writer.port, writer.database)
    if owner.role == writer.role or not same_database:
        raise RestoreFailed("config")
    return owner


def _check_owner(connection: Connection, role: str) -> None:
    record = connection.execute(_OWNER_CHECK, (role,)).fetchone()
    if record is None or not record[0] or record[1] or not record[2]:
        raise RestoreFailed("owner")


def _lock(connection: Connection, lock_timeout_ms: int) -> None:
    connection.execute("SELECT pg_catalog.set_config('lock_timeout', %s, true)", (f"{lock_timeout_ms}ms",))
    connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (MIGRATE_LOCK,))
    names = sorted(schema_tables(connection))
    connection.execute("LOCK TABLE " + ", ".join(f"hub.{name}" for name in names) + " IN ACCESS EXCLUSIVE MODE")


def _check_schema(connection: Connection, files: Sequence[ExportFile]) -> None:
    applied = [row[0] for row in connection.execute("SELECT checksum FROM hub.schema_version ORDER BY version")]
    if any(parsed.header["schema"] != applied for parsed in files):
        raise RestoreFailed("schema")


def _check_empty(connection: Connection) -> None:
    seeded = {rule.table for rule in RULES if rule.seeded}
    for name in sorted((set(RULE_BY_TABLE) | set(EXCLUDED)) - seeded - {"schema_version"}):
        record = connection.execute(f"SELECT EXISTS (SELECT 1 FROM hub.{name})").fetchone()
        if cast(tuple[bool], record)[0]:
            raise RestoreFailed("not_empty")


def insert(connection: Connection, merged: Mapping[str, Sequence[Row]], tables: Mapping[str, Table]) -> int:
    """Insert the merged rows in the order of `RULES`; the number of rows."""
    count = 0
    for rule in RULES:
        table = tables[rule.table]
        found = merged[rule.table]
        if rule.seeded and found:
            connection.execute(f"DELETE FROM hub.{table.name}")
        if not found:
            continue
        columns = ", ".join(f'"{name}"' for name in table.names)
        values = ", ".join("%s" for _ in table.names)
        with connection.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO hub.{table.name} ({columns}) VALUES ({values})",
                [decode_row(table, row) for row in found],
            )
        count += len(found)
    return count


class _Steps:
    """The name of the running step, for the refusal of an unexpected error (never its message)."""

    def __init__(self) -> None:
        self.name = "owner"

    def run(self, name: str, action: Callable[[], Any]) -> Any:
        self.name = name
        return action()


def restore(
    connection: Connection,
    owner_role: str,
    contents: Sequence[bytes],
    config: HubConfig,
    *,
    lock_timeout_ms: int = LOCK_TIMEOUT_MS,
) -> int:
    """Steps `owner` to `projections` in one transaction of the owner's connection; the number of rows restored."""
    steps = _Steps()
    signer = config.signer()
    try:
        with connection.transaction():
            steps.run("owner", lambda: _check_owner(connection, owner_role))
            steps.run("lock", lambda: _lock(connection, lock_timeout_ms))
            tables = {table.name: table for table in steps.run("schema", lambda: export_tables(connection))}
            files = steps.run("file", lambda: [parse_file(data, tables) for data in contents])
            steps.run("signature", lambda: check_signatures(files, signer.key_id, signer.public_key))
            steps.run("schema", lambda: _check_schema(connection, files))
            steps.run("not_empty", lambda: _check_empty(connection))
            merged = steps.run("conflict", lambda: merge(files, tables))
            steps.run("verify", lambda: verify(merged, config))
            count = cast(int, steps.run("insert", lambda: insert(connection, merged, tables)))
            result = steps.run(
                "projections", lambda: rebuild.compare(connection, config, rebuild.mutable_tables(connection))
            )
            if result.differences:
                raise RestoreFailed("projections")
    except RestoreFailed:
        raise
    except psycopg.errors.LockNotAvailable:
        raise RestoreFailed("lock") from None
    except Exception:
        # Never the exception itself: a database message or a malformed value could be printed.
        raise RestoreFailed(steps.name) from None
    return count
