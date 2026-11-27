"""The hash-chained audit and read log (02-data-model.md section 2.9).

`append_audit` gives every row the next `seq` without a gap under the transaction advisory lock AUDIT_LOCK and the
hash

    hash = sha256_hex("agent-audit-v1\\n" ‖ seq ‖ "\\n" ‖ canonical(row without hash fields) ‖ "\\n"
                      ‖ (prev_hash or "-"))

where "row without hash fields" is the audit row as the API returns it (`contracts/api/org-audit.response.json`:
`seq`, `at` with milliseconds, the IDs as lower-case text or null, `detail`, ...) and canonical() is the canonical
JSON of 04-crypto.md section 1. `detail` holds metadata only: a flat object of printable-ASCII strings (at most 256
characters), integers, booleans and nulls, and it fits the detail schema of its action (`contracts/audit/<action>.json`,
`agent_hub.audit.details`). `verify_chain` recomputes every hash and link of a range.

A row written while the request of an agent is processed (ADR-056) carries the agent's ID in `agent_id`: the request
pipeline sets `ACTING_AGENT` for the request, and `append_audit` fills it into a row that names no agent itself. The
hashed row JSON contains the key `agent_id` only when it is set, so the hashes of rows written before (and of every
row without an agent) are unchanged.

What a session does is attributed to its agent (ADR-095 section 4, KOR-52): `actor_kind` `main_agent` or `subagent`
with the session as `actor_id`, and `on_behalf_of` the person whose instruction it followed. Like `agent_id`, the key
`on_behalf_of` is part of the hashed row only when it is set, so every row written before keeps its hash; any other
actor kind with a person is refused.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agent_core.signing import sha256_hex
from agent_hub.audit.actions import AUDIT_ACTIONS
from agent_hub.audit.details import detail_violation
from agent_hub.audit.recorder import note
from agent_hub.crypto.encoding import MAX_SAFE_INTEGER, canonical, format_time, is_printable_ascii
from agent_hub.crypto.errors import CryptoError
from agent_hub.db.blocks import blocks, size_sql

AUDIT_LOCK = 742180101
HASH_LABEL = "agent-audit-v1"
NO_PREVIOUS = "-"
READ_BATCH = 1000
READ_BYTES = 8 * 1024 * 1024
"""About the most stored bytes `read_audit` holds per batch."""

ACTOR_KINDS = frozenset({"user", "device", "session", "hub", "operator", "anonymous", "main_agent", "subagent"})
RESULTS = frozenset({"ok", "denied", "rejected", "duplicate", "failed"})
AGENT_ACTORS = frozenset({"main_agent", "subagent"})
"""The actor kinds that act on behalf of a person (`on_behalf_of`, ADR-095 section 4)."""
ID_FIELDS = (
    "actor_id",
    "org_id",
    "wg_id",
    "task_id",
    "session_id",
    "device_id",
    "event_id",
    "agent_id",
    "on_behalf_of",
)
ACTING_AGENT: ContextVar[UUID | None] = ContextVar("agent_hub_acting_agent", default=None)
"""The agent whose request is being processed (set by the request pipeline, ADR-056)."""
MAX_DETAIL_KEYS = 32
MAX_DETAIL_TEXT = 256

_TARGET_KIND_RE = re.compile(r"[a-z_]{1,32}")
_TARGET_ID_RE = re.compile(r"[ -~]{1,128}")
_DETAIL_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Detail keys that would name confidential plaintext (00-overview.md section 3, 04-crypto.md section 7) are refused on
# top of the per-action detail schemas of contracts/audit/ (`agent_hub.audit.details`), as a second guard that holds
# even if a schema were widened by mistake.
_DETAIL_PLAINTEXT_RE = re.compile(
    r"(^|_)(title|name|description|prompt|answer|body|text|content|message|comment|query|url|email|token|secret|"
    r"password|credential|path|file|repo|branch|output|input)(_|$)"
)
UUID_VERSION = 7

DetailValue = str | int | bool | None

_COLUMNS = (
    "seq, at, actor_id, actor_kind, action, result, org_id, wg_id, task_id, session_id, device_id, target_kind,"
    " target_id, detail, event_id, prev_hash, hash, agent_id, on_behalf_of"
)
_SIZE = size_sql(
    [
        ("actor_kind", "text"),
        ("action", "text"),
        ("result", "text"),
        ("target_kind", "text"),
        ("target_id", "text"),
        ("detail", "jsonb"),
        ("prev_hash", "text"),
        ("hash", "text"),
    ]
)
"""The size of a stored audit row (`agent_hub.db.blocks.size_sql`; the other columns have a fixed size)."""


class AuditIsolationError(RuntimeError):
    """`append_audit` needs READ COMMITTED: a snapshot taken before the lock would read a stale head."""


class AuditRowInvalid(ValueError):
    """An audit row breaks the form of section 2.9; the message names the field, never its value."""


@dataclass(frozen=True)
class AuditRow:
    """One audit row before it is appended (no `seq` and no hash fields yet)."""

    at: datetime
    actor_kind: str
    action: str
    result: str
    actor_id: UUID | None = None
    org_id: UUID | None = None
    wg_id: UUID | None = None
    task_id: UUID | None = None
    session_id: UUID | None = None
    device_id: UUID | None = None
    target_kind: str | None = None
    target_id: str | None = None
    detail: Mapping[str, DetailValue] = field(default_factory=dict)
    event_id: UUID | None = None
    agent_id: UUID | None = None
    on_behalf_of: UUID | None = None
    """The person a `main_agent`/`subagent` actor acted for (ADR-095 section 4)."""


@dataclass(frozen=True)
class StoredAudit:
    """An appended audit row with its position and hash fields."""

    seq: int
    row: AuditRow
    prev_hash: str | None
    hash: str


@dataclass(frozen=True)
class ChainCheck:
    """Result of a verification: `broken_seq` is the first row that does not verify, None when all do.

    `head_seq`/`head_hash` describe the last verified row of the range (0/None for an empty range).
    """

    ok: bool
    head_seq: int
    head_hash: str | None
    broken_seq: int | None


def to_millis(moment: datetime) -> datetime:
    """`moment` in UTC, truncated to whole milliseconds (the precision of `at`); naive times are refused."""
    if moment.tzinfo is None:
        raise AuditRowInvalid("at must carry a time zone")
    utc = moment.astimezone(UTC)
    return utc.replace(microsecond=utc.microsecond - utc.microsecond % 1000)


def _detail_value_valid(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
    return isinstance(value, str) and len(value) <= MAX_DETAIL_TEXT and is_printable_ascii(value)


def validate_row(row: AuditRow) -> None:
    """Refuse a row outside the form of section 2.9 and the audit row contract (metadata only)."""
    if row.actor_kind not in ACTOR_KINDS:
        raise AuditRowInvalid("actor_kind")
    if row.action not in AUDIT_ACTIONS:
        raise AuditRowInvalid("action")
    if row.result not in RESULTS:
        raise AuditRowInvalid("result")
    if row.on_behalf_of is not None and row.actor_kind not in AGENT_ACTORS:
        raise AuditRowInvalid("on_behalf_of")
    for name in ID_FIELDS:
        value = getattr(row, name)
        if value is not None and (not isinstance(value, UUID) or value.version != UUID_VERSION):
            raise AuditRowInvalid(name)
    if row.target_kind is not None and _TARGET_KIND_RE.fullmatch(row.target_kind) is None:
        raise AuditRowInvalid("target_kind")
    if row.target_id is not None and _TARGET_ID_RE.fullmatch(row.target_id) is None:
        raise AuditRowInvalid("target_id")
    if not isinstance(row.detail, Mapping) or len(row.detail) > MAX_DETAIL_KEYS:
        raise AuditRowInvalid("detail")
    for key, value in row.detail.items():
        if (
            not isinstance(key, str)
            or _DETAIL_KEY_RE.fullmatch(key) is None
            or _DETAIL_PLAINTEXT_RE.search(key) is not None
            or not _detail_value_valid(value)
        ):
            raise AuditRowInvalid("detail")
    violation = detail_violation(row.action, row.detail)
    if violation is not None:
        raise AuditRowInvalid(f"detail.{violation} of {row.action}")
    to_millis(row.at)


def _id_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def row_json(seq: int, row: AuditRow) -> dict[str, Any]:
    """The audit row without its hash fields, as the API returns it and as the hash covers it (`agent_id` and
    `on_behalf_of` only when set)."""
    found: dict[str, Any] = {
        "seq": seq,
        "at": format_time(to_millis(row.at)),
        "actor_id": _id_text(row.actor_id),
        "actor_kind": row.actor_kind,
        "action": row.action,
        "result": row.result,
        "org_id": _id_text(row.org_id),
        "wg_id": _id_text(row.wg_id),
        "task_id": _id_text(row.task_id),
        "session_id": _id_text(row.session_id),
        "device_id": _id_text(row.device_id),
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "detail": dict(row.detail),
        "event_id": _id_text(row.event_id),
    }
    if row.agent_id is not None:
        found["agent_id"] = str(row.agent_id)
    if row.on_behalf_of is not None:
        found["on_behalf_of"] = str(row.on_behalf_of)
    return found


def hash_text(seq: int, row: AuditRow, prev_hash: str | None) -> bytes:
    """The bytes the hash of a row covers."""
    return b"\n".join(
        [
            HASH_LABEL.encode("ascii"),
            str(seq).encode("ascii"),
            canonical(row_json(seq, row)),
            (prev_hash or NO_PREVIOUS).encode("ascii"),
        ]
    )


def audit_hash(seq: int, row: AuditRow, prev_hash: str | None) -> str:
    """The hash of section 2.9 (lower-case hex SHA-256)."""
    return sha256_hex(hash_text(seq, row, prev_hash))


def append_audit(connection: psycopg.Connection[Any], row: AuditRow) -> StoredAudit:
    """Append one row inside the connection's transaction (a new one when none is open).

    The advisory lock AUDIT_LOCK is held until the outermost transaction ends, so concurrent writers get gapless,
    strictly ordered `seq` values and each row links to the hash of its predecessor.
    """
    if row.agent_id is None:
        row = replace(row, agent_id=ACTING_AGENT.get())
    validate_row(row)
    stored_row = replace(row, at=to_millis(row.at), detail=dict(row.detail))
    with connection.transaction():
        isolation = connection.execute("SELECT pg_catalog.current_setting('transaction_isolation')").fetchone()
        if isolation != ("read committed",):
            raise AuditIsolationError("append_audit needs READ COMMITTED")
        connection.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
        head = connection.execute("SELECT seq, hash FROM hub.audit ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev_hash = (1, None) if head is None else (int(head[0]) + 1, str(head[1]))
        digest = audit_hash(seq, stored_row, prev_hash)
        connection.execute(
            f"INSERT INTO hub.audit ({_COLUMNS})"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                seq,
                stored_row.at,
                stored_row.actor_id,
                stored_row.actor_kind,
                stored_row.action,
                stored_row.result,
                stored_row.org_id,
                stored_row.wg_id,
                stored_row.task_id,
                stored_row.session_id,
                stored_row.device_id,
                stored_row.target_kind,
                stored_row.target_id,
                Jsonb(dict(stored_row.detail)),
                stored_row.event_id,
                prev_hash,
                digest,
                stored_row.agent_id,
                stored_row.on_behalf_of,
            ),
        )
    note(seq, digest, stored_row.result)
    return StoredAudit(seq=seq, row=stored_row, prev_hash=prev_hash, hash=digest)


def _stored(record: Sequence[Any]) -> StoredAudit:
    row = AuditRow(
        at=cast(datetime, record[1]),
        actor_id=record[2],
        actor_kind=record[3],
        action=record[4],
        result=record[5],
        org_id=record[6],
        wg_id=record[7],
        task_id=record[8],
        session_id=record[9],
        device_id=record[10],
        target_kind=record[11],
        target_id=record[12],
        detail=record[13],
        event_id=record[14],
        agent_id=record[17],
        on_behalf_of=record[18],
    )
    return StoredAudit(seq=int(record[0]), row=row, prev_hash=record[15], hash=record[16])


def read_audit(
    connection: psycopg.Connection[Any], from_seq: int = 1, to_seq: int | None = None
) -> Iterator[StoredAudit]:
    """The stored rows with `from_seq <= seq <= to_seq` (to the head when None), ascending, read in batches of at most
    `READ_BATCH` rows and about `READ_BYTES` stored bytes (`agent_hub.db.blocks`; a larger row alone): the sizes of the
    next rows are read first, then the rows of the first block. So reading the whole chain needs memory for one batch,
    whatever the length of the chain or the size of a row (HOT-27)."""
    last = from_seq - 1
    while True:
        sizes = connection.execute(
            f"SELECT seq, {_SIZE} FROM hub.audit WHERE seq > %s AND (%s::bigint IS NULL OR seq <= %s)"
            " ORDER BY seq LIMIT %s",
            (last, to_seq, to_seq, READ_BATCH),
        ).fetchall()
        if not sizes:
            return
        count = next(blocks((int(size) for _seq, size in sizes), max_rows=READ_BATCH, max_bytes=READ_BYTES))
        through = int(sizes[count - 1][0])
        records = connection.execute(
            f"SELECT {_COLUMNS} FROM hub.audit WHERE seq > %s AND seq <= %s ORDER BY seq", (last, through)
        ).fetchall()
        for record in records:
            yield _stored(record)
        last = through


def verify_rows(
    rows: Iterator[StoredAudit] | Sequence[StoredAudit], first_seq: int, prev_hash: str | None
) -> ChainCheck:
    """Verify consecutive rows starting at `first_seq`, whose predecessor has the hash `prev_hash`.

    Every row must have the next `seq`, link to the hash of its predecessor (none for seq 1) and carry the hash
    recomputed from its fields. Works on any copy of the rows (export, backup, a list).
    """
    expected_seq = first_seq
    head_seq, head_hash = first_seq - 1, prev_hash
    for stored in rows:
        if stored.seq != expected_seq or stored.prev_hash != head_hash or stored.hash != _hash_or_none(stored):
            return ChainCheck(ok=False, head_seq=head_seq, head_hash=head_hash, broken_seq=expected_seq)
        head_seq, head_hash = stored.seq, stored.hash
        expected_seq += 1
    return ChainCheck(ok=True, head_seq=head_seq, head_hash=head_hash, broken_seq=None)


def _hash_or_none(stored: StoredAudit) -> str | None:
    """The recomputed hash of a stored row; None when a tampered row cannot even be hashed (float, non-ASCII, ...)."""
    try:
        return audit_hash(stored.seq, stored.row, stored.prev_hash)
    except (ValueError, TypeError, CryptoError):
        return None


def verify_chain(connection: psycopg.Connection[Any], from_seq: int = 1, to_seq: int | None = None) -> ChainCheck:
    """Verify the stored rows `from_seq..to_seq` (to the head when None) against their predecessor."""
    if from_seq < 1:
        raise ValueError("from_seq starts at 1")
    prev_hash: str | None = None
    if from_seq > 1:
        anchor = connection.execute("SELECT hash FROM hub.audit WHERE seq = %s", (from_seq - 1,)).fetchone()
        if anchor is None:
            return ChainCheck(ok=False, head_seq=from_seq - 1, head_hash=None, broken_seq=from_seq - 1)
        prev_hash = str(anchor[0])
    check = verify_rows(read_audit(connection, from_seq, to_seq), from_seq, prev_hash)
    if check.ok and to_seq is not None and check.head_seq < to_seq:
        # Rows the caller expects are missing (a cut-off tail).
        return replace(check, ok=False, broken_seq=check.head_seq + 1)
    return check
