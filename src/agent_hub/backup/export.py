"""`agent-hub export --org <id>`: the JSON Lines file `agent-hub-export-v1` of one org (02-data-model.md section 3).

The file is written in the writer's form of `agent_core.record.export`: one compact JSON object per line (`,` and `:`
without spaces, ASCII only, members in a fixed order), every line ending in `\\n`:

1. the header `{"format","org_id","exported_at","hub_key_id","schema","log_head","platform_head","audit_head"}` -
   `schema` lists the checksums of the applied migrations in version order, the heads are `{"seq","hash"}` (the
   org's membership log; the platform log and the audit chain, null while empty) and name the state of the hub-wide
   chains the file carries ("platform-log reference");
2. one line `{"table","row"}` per row, tables in the order of `agent_hub.backup.tables.RULES`, rows by primary key,
   `row` members in column order, values in the forms of `agent_hub.backup.tables`;
3. the trailer `{"trailer","rows","sha256","signature"}`: `trailer` is the format name, `sha256` the lower-case hex
   SHA-256 of every byte before the trailer, `signature` the hub key's Ed25519 signature (b64u) of
   `agent-hub-export-v1\\n<hub key id>\\n<org id>\\n<rows>\\n<sha256>`. `agent-hub restore` refuses a file whose
   bytes or signature do not verify, so no line can be changed, added or removed unnoticed (decision DR-01 2).

The rows are read in one `REPEATABLE READ, READ ONLY` transaction of the writer role: the file is one consistent
snapshot while the hub keeps running. The hub stores only ciphertext and the metadata of 04-crypto.md section 7
(02-data-model.md section 1.1), and the export copies stored values unchanged, so the file holds no plaintext.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO
from uuid import UUID

import psycopg

from agent_core.signing import HubSigner
from agent_hub.backup.tables import FORMAT, RULE_BY_TABLE, Table, encode_row, export_tables, format_timestamp

Connection = psycopg.Connection[Any]
FETCH_ROWS = 500
HEADER_ORDER = (
    "format",
    "org_id",
    "exported_at",
    "hub_key_id",
    "schema",
    "log_head",
    "platform_head",
    "audit_head",
)
TRAILER_ORDER = ("trailer", "rows", "sha256", "signature")


class OrgUnknown(LookupError):
    """No org with this ID exists."""


def line(value: dict[str, Any]) -> bytes:
    """One line in the writer's form."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"


def signed_text(hub_key_id: str, org_id: str, rows: int, digest: str) -> bytes:
    """The text the trailer signature covers."""
    return "\n".join([FORMAT, hub_key_id, org_id, str(rows), digest]).encode("ascii")


@dataclass(frozen=True)
class Selection:
    """The ID sets that select the rows of one org (`agent_hub.backup.tables.RULES`)."""

    org: UUID
    wgs: list[UUID]
    users: list[UUID]
    devices: list[UUID]
    tasks: list[UUID]
    sessions: list[UUID]
    operations: list[UUID]

    def params(self) -> dict[str, object]:
        return {
            "org": self.org,
            "wgs": self.wgs,
            "users": self.users,
            "devices": self.devices,
            "principals": sorted(set(self.users) | set(self.devices)),
            "tasks": self.tasks,
            "sessions": self.sessions,
            "operations": self.operations,
        }


def _ids(connection: Connection, query: str, org: UUID) -> list[UUID]:
    return sorted({row[0] for row in connection.execute(query, {"org": org}) if row[0] is not None})


def select(connection: Connection, org: UUID) -> Selection:
    """The ID sets of the org: its workgroups, tasks, sessions, operations; its users (members at any time and users
    registered through one of its invitations) and devices (members at any time, devices it owns, every platform
    device: the platform log certifies them hub-wide). Users are never selected for a platform role alone, so no
    user's identity rows reach the export of an org they do not belong to (decision DR-01 1)."""
    users = _ids(
        connection,
        "SELECT principal_id FROM hub.org_member WHERE org_id = %(org)s AND kind = 'user'"
        " UNION SELECT registered_user FROM hub.invite WHERE org_id = %(org)s",
        org,
    )
    devices = _ids(
        connection,
        "SELECT principal_id FROM hub.org_member WHERE org_id = %(org)s AND kind = 'device'"
        " UNION SELECT id FROM hub.device WHERE kind = 'platform' OR owner_org = %(org)s",
        org,
    )
    return Selection(
        org=org,
        wgs=_ids(connection, "SELECT id FROM hub.wg WHERE org_id = %(org)s", org),
        users=users,
        devices=devices,
        tasks=_ids(connection, "SELECT id FROM hub.task WHERE org_id = %(org)s", org),
        sessions=_ids(connection, "SELECT id FROM hub.session WHERE org_id = %(org)s", org),
        operations=_ids(connection, "SELECT id FROM hub.operation WHERE org_id = %(org)s", org),
    )


def _head(record: tuple[Any, ...] | None) -> dict[str, object] | None:
    return None if record is None else {"seq": int(record[0]), "hash": str(record[1])}


def header(connection: Connection, org: UUID, hub_key_id: str, now: datetime) -> dict[str, object]:
    """The header line of the org's export; OrgUnknown for an unknown org."""
    found = connection.execute("SELECT log_head_seq, log_head_hash FROM hub.org WHERE id = %s", (org,)).fetchone()
    if found is None:
        raise OrgUnknown("unknown org")
    schema = [row[0] for row in connection.execute("SELECT checksum FROM hub.schema_version ORDER BY version")]
    platform = connection.execute("SELECT seq, entry_hash FROM hub.platform_entry ORDER BY seq DESC LIMIT 1")
    audit = connection.execute("SELECT seq, hash FROM hub.audit ORDER BY seq DESC LIMIT 1")
    values: dict[str, object] = {
        "format": FORMAT,
        "org_id": str(org),
        "exported_at": format_timestamp(now),
        "hub_key_id": hub_key_id,
        "schema": schema,
        "log_head": _head(found),
        "platform_head": _head(platform.fetchone()),
        "audit_head": _head(audit.fetchone()),
    }
    return {name: values[name] for name in HEADER_ORDER}


def rows(connection: Connection, table: Table, selection: Selection) -> Iterator[dict[str, object]]:
    """The org's rows of one table in primary-key order (read with a server-side cursor)."""
    rule = RULE_BY_TABLE[table.name]
    columns = ", ".join(f'"{name}"' for name in table.names)
    order = ", ".join(f'"{name}"' for name in table.key)
    query = f"SELECT {columns} FROM hub.{table.name} WHERE {rule.where} ORDER BY {order}"
    with connection.cursor(name=f"export_{table.name}") as cursor:
        cursor.itersize = FETCH_ROWS
        cursor.execute(query, selection.params())
        for record in cursor:
            yield encode_row(table, record)


def write_export(connection: Connection, org: UUID, signer: HubSigner, out: BinaryIO, now: datetime) -> int:
    """Write the export of `org` to `out`; the number of rows. Runs its own snapshot transaction."""
    digest = hashlib.sha256()

    def emit(data: bytes) -> None:
        digest.update(data)
        out.write(data)

    count = 0
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        emit(line(header(connection, org, signer.key_id, now)))
        tables = export_tables(connection)
        selection = select(connection, org)
        for table in tables:
            for row in rows(connection, table, selection):
                emit(line({"table": table.name, "row": row}))
                count += 1
    sha256 = digest.hexdigest()
    signature = signer.sign(signed_text(signer.key_id, str(org), count, sha256))
    out.write(line({"trailer": FORMAT, "rows": count, "sha256": sha256, "signature": signature}))
    return count
