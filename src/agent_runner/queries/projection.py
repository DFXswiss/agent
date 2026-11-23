"""The local view the session reads answer from (03-api.md sections 8.1 and 10, ADR-024, 04-crypto.md section 5.12,
05-runner.md sections 2.2 and 2.4; decisions/RUN-08.md).

The inbox hands every verified event of `KINDS` to this area's consumer (`consumers.py`, ADR-039); `keep` stores it
in `runner.read_event` exactly as received (ciphertext and signature) with the author's verified signing key and the
clear header fields the reads select by. Plaintext exists only in memory while a read is answered: `open_body`
opens a stored event with the scope key of its epoch from the store (and checks the author signature again).

- `messages`: the chat items of one task of one workgroup scope - `prompt` and `session.output` in the hub's scope
  order (`seq` = `scope_seq`), from `after`, at most `limit`, within the answer budget.
- `entries`: the whiteboard of one level (org scope; workgroup scope without task; workgroup scope of one task),
  replayed like the hub projection (`agent_hub.whiteboards.projection`): a `whiteboard.entry` creates the entry
  (its `ref`; the first one wins, also across levels by the hub receive time),
  `whiteboard.edit` replaces the text and `whiteboard.delete` removes it, both only
  for an existing, not deleted entry of the same level and task. Newest entry first, within the answer budget.
- `device_label`: the label of the latest `device.profile` of a device ("latest" compares the signed `ts`, then the
  event ID, never the arrival order, ADR-039).

The answer to the guest is one JSON body of at most 1 MiB (`POST /v1/pending/{id}/answer`), so list results stop at
`BUDGET` bytes of JSON text (the body as the guest link encodes it, non-ASCII escaped); a single chat item that is
larger on its own is cut to fit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.orgs.keys import load_scope_key

PROMPT, OUTPUT = "prompt", "session.output"
ENTRY, EDIT, DELETE = "whiteboard.entry", "whiteboard.edit", "whiteboard.delete"
DEVICE_PROFILE = "device.profile"
MESSAGE_KINDS = (PROMPT, OUTPUT)
BOARD_KINDS = (ENTRY, EDIT, DELETE)
KINDS = frozenset({*MESSAGE_KINDS, *BOARD_KINDS, DEVICE_PROFILE})
ORG_LEVEL, WG_LEVEL, TASK_LEVEL = "org", "workgroup", "task"
BUDGET = 768 * 1024
"""JSON bytes of the listed items of one answer (the guest takes answer bodies up to 1 MiB)."""
MAX_ENTRIES = 1000
MAX_ENTRY_TEXT, MAX_MESSAGE_TEXT = 65536, 262144
"""Character limits of `text` in `mcp/tools/get_whiteboard.output.json` and `get_task_messages.output.json`: the hub
cannot check plaintext sizes, so a longer text from a writer is cut here instead of breaking the whole answer."""
_TIME_RE = re.compile(
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_COLUMNS = (
    "event_id::text AS event_id, kind, scope, scope_id::text AS scope_id, scope_seq, task_id::text AS task_id,"
    " ref::text AS ref, author_id::text AS author_id, ts, received_at, author_sig, sealed"
)


@dataclass(frozen=True)
class Stored:
    """One kept event: clear header fields, the event as received and the author's signing key."""

    event_id: str
    kind: str
    scope: str
    scope_id: str
    scope_seq: int
    task_id: str | None
    ref: str | None
    author_id: str
    ts: str
    received_at: str
    author_sig: str
    sealed: Mapping[str, Any]


def json_size(value: object) -> int:
    """Bytes of `value` as JSON text the way the guest link sends it (ASCII, non-ASCII escaped)."""
    return len(json.dumps(value, separators=(",", ":")))


def _object(header: Mapping[str, object], name: str) -> str | None:
    value = header.get(name)
    return value if isinstance(value, str) and _UUID_RE.fullmatch(value) else None


def keepable(header: Mapping[str, object]) -> bool:
    """A chat item names its task; whiteboard and device-profile events name their object (`ref`)."""
    kind = header.get("kind")
    if kind in MESSAGE_KINDS:
        return _object(header, "task") is not None
    return kind in KINDS and _object(header, "ref") is not None


async def keep(
    conn: AsyncConnection[Any],
    *,
    header: Mapping[str, object],
    scope_seq: int,
    received_at: str,
    author_sig: str,
    sealed: Mapping[str, object],
) -> bool:
    """Store one verified event (`keepable`); True when stored, False for a repeat (same event ID or scope order)."""
    cur = await conn.execute(
        "INSERT INTO runner.read_event (event_id, kind, org_id, scope, scope_id, scope_seq, task_id, ref, author_id,"
        " ts, received_at, author_sig, sealed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (
            header["id"],
            header["kind"],
            header["org"],
            header["scope"],
            header["scope_id"],
            scope_seq,
            _object(header, "task"),
            _object(header, "ref"),
            header["author"],
            header["ts"],
            received_at if _TIME_RE.fullmatch(received_at) else header["ts"],
            author_sig,
            Jsonb(dict(sealed)),
        ),
    )
    return cur.rowcount == 1


async def open_body(conn: AsyncConnection[Any], storage_key: bytes, row: Stored) -> Mapping[str, Any] | None:
    """The decrypted body of a kept event; None when the device holds no key for its epoch or it does not open."""
    try:
        header = row.sealed["header"]
        key = await load_scope_key(conn, storage_key, str(header["scope"]), str(header["scope_id"]), header["epoch"])
        if key is None:
            return None
        body = parse_json(open_event(key, row.sealed, b64u_decode(row.author_sig)))
    except (CryptoError, KeyError, TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _text(body: Mapping[str, Any] | None) -> str | None:
    value = None if body is None else body.get("text")
    return value if isinstance(value, str) else None


async def _rows(conn: AsyncConnection[Any], where: str, params: Sequence[object]) -> list[Stored]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM runner.read_event WHERE {where}", params)
        return [Stored(**row) for row in await cur.fetchall()]


def cut(text: str, room: int) -> str:
    """The longest prefix of `text` whose JSON string fits into `room` bytes."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if json_size(text[:middle]) <= room:
            low = middle
        else:
            high = middle - 1
    return text[:low]


async def messages(
    conn: AsyncConnection[Any],
    storage_key: bytes,
    *,
    wg_id: str,
    task_id: str,
    after: int,
    limit: int,
    budget: int = BUDGET,
) -> tuple[list[dict[str, Any]], int | None]:
    """`(items, next_after)` of the chat of a task (module docstring); `next_after` None when nothing follows."""
    rows = await _rows(
        conn,
        "scope_id = %s AND task_id = %s AND kind = ANY(%s) AND scope_seq > %s ORDER BY scope_seq LIMIT %s",
        (wg_id, task_id, list(MESSAGE_KINDS), after, limit + 1),
    )
    items: list[dict[str, Any]] = []
    used = 0
    last: int | None = None
    for row in rows[:limit]:
        text = _text(await open_body(conn, storage_key, row))
        if text is not None:
            text = text[:MAX_MESSAGE_TEXT]
            item = {
                "seq": row.scope_seq,
                "kind": "prompt" if row.kind == PROMPT else "output",
                "author_id": row.author_id,
                "ts": row.ts,
                "text": text,
            }
            size = json_size(item)
            if used + size > budget:
                if items:
                    return items, last
                item["text"] = cut(text, budget - (size - json_size(text)))
                size = json_size(item)
            items.append(item)
            used += size
        last = row.scope_seq
    return items, (last if len(rows) > limit else None)


@dataclass
class _Entry:
    entry_id: str
    created_id: str
    author_id: str
    created_seq: int
    created_at: str
    latest: Stored
    deleted: bool = False


def _replay(rows: Sequence[Stored]) -> list[_Entry]:
    entries: dict[str, _Entry] = {}
    for row in rows:
        assert row.ref is not None
        found = entries.get(row.ref)
        if row.kind == ENTRY:
            if found is None:
                entries[row.ref] = _Entry(row.ref, row.event_id, row.author_id, row.scope_seq, row.received_at, row)
        elif found is not None and not found.deleted:
            found.latest = row
            found.deleted = row.kind == DELETE
    return sorted((e for e in entries.values() if not e.deleted), key=lambda e: e.created_seq, reverse=True)


async def _first_with_id(conn: AsyncConnection[Any], entry: _Entry) -> bool:
    """The entry's creating event is the first kept `whiteboard.entry` with its ID on any level (hub receive time,
    then event ID): entry IDs are unique across levels at the hub, a later entry with a used ID is never projected."""
    cur = await conn.execute(
        "SELECT event_id::text FROM runner.read_event WHERE kind = %s AND ref = %s"
        " ORDER BY received_at, event_id LIMIT 1",
        (ENTRY, entry.entry_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0] == entry.created_id)


async def entries(
    conn: AsyncConnection[Any],
    storage_key: bytes,
    *,
    level: str,
    scope_id: str,
    task_id: str | None = None,
    budget: int = BUDGET,
) -> list[dict[str, Any]]:
    """The live entries of one whiteboard level, newest first (module docstring)."""
    where = "scope_id = %s AND kind = ANY(%s)"
    params: list[object] = [scope_id, list(BOARD_KINDS)]
    if level == ORG_LEVEL:
        where += " AND scope = 'org'"
    elif level == WG_LEVEL:
        where += " AND scope = 'wg' AND task_id IS NULL"
    else:
        where += " AND scope = 'wg' AND task_id = %s"
        params.append(task_id)
    found: list[dict[str, Any]] = []
    used = 0
    for entry in _replay(await _rows(conn, where + " ORDER BY scope_seq", params)):
        text = _text(await open_body(conn, storage_key, entry.latest))
        if text is None or not await _first_with_id(conn, entry):
            continue
        text = text[:MAX_ENTRY_TEXT]
        item = {
            "entry_id": entry.entry_id,
            "author_id": entry.author_id,
            "created_at": entry.created_at,
            "updated_at": entry.latest.received_at,
            "text": text,
        }
        used += json_size(item)
        if used > budget or len(found) == MAX_ENTRIES:
            break
        found.append(item)
    return found


async def device_label(conn: AsyncConnection[Any], storage_key: bytes, org_id: str, device_id: str) -> str | None:
    """The label of the latest kept `device.profile` of the device; None when none opens."""
    rows = await _rows(
        conn,
        "kind = %s AND org_id = %s AND ref = %s ORDER BY ts DESC, event_id DESC LIMIT 1",
        (DEVICE_PROFILE, org_id, device_id),
    )
    body = await open_body(conn, storage_key, rows[0]) if rows else None
    label = None if body is None else body.get("label")
    return label if isinstance(label, str) and label.strip() else None
