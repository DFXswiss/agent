"""Names for the harness context of a session (05-runner.md section 8, 03-api.md section 10, ADR-039).

The names are confidential: they arrive only inside events (`org.profile`, `wg.profile`, `member.profile`,
`task.meta`, `task.agent_name`). The inbox hands every verified event of these kinds to the consumer of this area
(`sessions/consumers.py`); `remember` keeps the latest one per object in `runner.profile_event` exactly as received
(ciphertext and signature) together with the author's verified signing key. "Latest" compares the signed header
(`ts`, then the event ID), never the arrival order, so a replayed old event never replaces a newer one (ADR-039).
A main agent's name (`task.agent_name`, ADR-095 section 2) is the newest by the scope sequence number the inbox verified
it at, as every reader of the name decides (migration 0036); the inbox hands over verified events only, so the stored
one is the newest verified event (KOR-52).

`names(...)` opens the stored events with the scope keys of the store, only into memory, when a session starts. The
task title is taken only from a `task.meta` of the session's own workgroup (`scope_id`): the task ID comes from the
hub, which must not be able to make a session read the title of another workgroup's task (00-overview.md section 3,
B4). The store keeps the latest event per object *and scope* (key `(kind, org_id, scope_id, object_id)`, migration
0009, decisions KOR-03): a `task.meta` of another workgroup that names the same task ID is kept apart and never
displaces the title of the session's workgroup. Org-scoped kinds are read from the org scope (`scope_id = org`,
04-crypto.md section 5.4).
Objects of the events: `org.profile` -> the org, `wg.profile` -> header `wg`, `member.profile` -> header `ref` (the
member), `task.meta` and `task.agent_name` -> header `task`. `given_name` reads the main agent's name: None without an
event, for a body naming null (back to the automatic name) and for one that does not open or breaks the body schema
(an older event is never used instead, ADR-095 section 2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.crypto.encoding import b64u_decode, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import open_event
from agent_runner.orgs.keys import load_scope_key

AGENT_NAME = "task.agent_name"
KINDS = frozenset({"org.profile", "wg.profile", "member.profile", "task.meta", AGENT_NAME})
# Kinds whose events live in the workgroup scope (contracts/events/kinds.json); the others are org-scoped.
_WG_KINDS = frozenset({"task.meta", AGENT_NAME})
_OBJECT_FIELD = {
    "org.profile": "org",
    "wg.profile": "wg",
    "member.profile": "ref",
    "task.meta": "task",
    AGENT_NAME: "task",
}
_NAME_FIELD = {
    "org.profile": "name",
    "wg.profile": "name",
    "member.profile": "display_name",
    "task.meta": "title",
}
# `events/objects.json#/$defs/body_task_agent_name`: 1-80 characters on one line.
_AGENT_NAME_RE = re.compile("[^\r\n]{1,80}")


def object_of(kind: str, header: Mapping[str, object]) -> str:
    return str(header[_OBJECT_FIELD[kind]])


async def remember(
    conn: AsyncConnection[Any],
    *,
    header: Mapping[str, object],
    sealed: Mapping[str, object],
    author_sig: str,
    scope_seq: int | None = None,
) -> bool:
    """Keep the event when it is newer than the stored one of its object; True when stored.

    `scope_seq` is the event's verified position in its scope; a `task.agent_name` needs it (it decides "newer")."""
    kind = str(header["kind"])
    if kind == AGENT_NAME and scope_seq is None:
        raise ValueError(f"{kind} needs its scope_seq")
    cur = await conn.execute(
        "INSERT INTO runner.profile_event"
        " (kind, org_id, scope_id, object_id, event_id, ts, author_sig, sealed, scope_seq)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (kind, org_id, scope_id, object_id) DO UPDATE SET"
        " event_id = EXCLUDED.event_id, ts = EXCLUDED.ts, author_sig = EXCLUDED.author_sig, sealed = EXCLUDED.sealed,"
        " scope_seq = EXCLUDED.scope_seq"
        " WHERE CASE WHEN EXCLUDED.kind = 'task.agent_name'"
        " THEN EXCLUDED.scope_seq > runner.profile_event.scope_seq"
        " ELSE (EXCLUDED.ts, EXCLUDED.event_id) > (runner.profile_event.ts, runner.profile_event.event_id) END",
        (
            kind,
            header["org"],
            header["scope_id"],
            object_of(kind, header),
            header["id"],
            header["ts"],
            author_sig,
            Jsonb(dict(sealed)),
            scope_seq,
        ),
    )
    return cur.rowcount == 1


async def _body(
    conn: AsyncConnection[Any], storage_key: bytes, kind: str, org_id: str, object_id: str, *, scope_id: str | None
) -> object:
    """The opened body of the latest stored event of the object in scope `scope_id`; None when none is stored or it
    does not open."""
    if scope_id is None:
        if kind in _WG_KINDS:
            raise ValueError(f"{kind} needs the workgroup as scope_id")
        scope_id = org_id
    cur = await conn.execute(
        "SELECT author_sig, sealed FROM runner.profile_event"
        " WHERE kind = %s AND org_id = %s AND scope_id = %s AND object_id = %s",
        (kind, org_id, scope_id, object_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    author_sig, sealed = str(row[0]), row[1]
    try:
        header = sealed["header"]
        if header["scope_id"] != scope_id:
            return None
        key = await load_scope_key(conn, storage_key, str(header["scope"]), str(header["scope_id"]), header["epoch"])
        if key is None:
            return None
        return parse_json(open_event(key, sealed, b64u_decode(author_sig)))
    except (CryptoError, KeyError, TypeError, ValueError):
        return None


async def name(
    conn: AsyncConnection[Any],
    storage_key: bytes,
    kind: str,
    org_id: str,
    object_id: str,
    *,
    scope_id: str | None = None,
) -> str | None:
    """The name field of the latest stored event of the object in scope `scope_id`; None when none is stored or it
    does not open.

    `scope_id` is the session's workgroup for `task.meta` (required) and defaults to the org for the org-scoped kinds.
    """
    body = await _body(conn, storage_key, kind, org_id, object_id, scope_id=scope_id)
    value = body.get(_NAME_FIELD[kind]) if isinstance(body, dict) else None
    return value if isinstance(value, str) and value.strip() else None


async def given_name(
    conn: AsyncConnection[Any], storage_key: bytes, org_id: str, task_id: str, *, wg_id: str
) -> str | None:
    """The name a person gave the main agent of `task_id` (module docstring), None when it is automatic."""
    body = await _body(conn, storage_key, AGENT_NAME, org_id, task_id, scope_id=wg_id)
    value = body.get("name") if isinstance(body, dict) and set(body) == {"name"} else None
    return value if isinstance(value, str) and _AGENT_NAME_RE.fullmatch(value) else None


@dataclass(frozen=True)
class Names:
    org: str | None
    workgroup: str | None
    task: str | None
    owner: str | None


async def names(
    conn: AsyncConnection[Any], storage_key: bytes, *, org_id: str, wg_id: str, task_id: str, owner_id: str
) -> Names:
    return Names(
        org=await name(conn, storage_key, "org.profile", org_id, org_id),
        workgroup=await name(conn, storage_key, "wg.profile", org_id, wg_id),
        task=await name(conn, storage_key, "task.meta", org_id, task_id, scope_id=wg_id),
        owner=await name(conn, storage_key, "member.profile", org_id, owner_id),
    )
