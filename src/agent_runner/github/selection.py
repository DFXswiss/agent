"""GitHub identity choices and defaults of the desired state (KOR-47, ADR-078 section 3; 03-api.md section 6.1
`GET /v1/device/state`; 02-data-model.md section 4 `github_selection`, `github_default`).

The hub sends, per session, the session's own identity (`github_identity`) and its task's (`task_github_identity`),
and the defaults of the organizations and workgroups the device is assigned to (`github_defaults[{scope, scope_id,
identity_id}]`). `store(conn, desired)` keeps the last answer: one row per listed session (rows of sessions the
answer no longer lists are deleted) and the complete default list. A missing field (a hub from before KOR-47) counts
as none. The values are only IDs; whether the acting human may use an identity is decided by the runner from its
verified logs at every operation (`identity.usable`), so a hub that swaps a choice can only pick an identity that human
may use anyway. A malformed value fails the reconcile step (`wire.ResponseInvalid`).

Because the answer is kept in the store, a restarted daemon that cannot reach the hub never falls back to another
account. `choices(conn, session)` and `defaults(conn, org_id, wg_id)` read it for the resolution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner.reconcile import wire

SCOPES = frozenset({"org", "wg"})


@dataclass(frozen=True)
class Choices:
    session: str | None
    task: str | None


def _optional(data: Mapping[str, Any], name: str, what: str) -> str | None:
    value = data.get(name)
    return None if value is None else wire.uuid_text(value, what)


def parse_sessions(answer: Mapping[str, Any]) -> list[tuple[str, str, str | None, str | None]]:
    """`(session_id, task_id, github_identity, task_github_identity)` of every listed session."""
    found = []
    for item in wire.array(answer.get("sessions", []), "sessions"):
        data = wire.obj(item, "sessions")
        found.append(
            (
                wire.uuid_text(data.get("session_id"), "sessions"),
                wire.uuid_text(data.get("task_id"), "sessions"),
                _optional(data, "github_identity", "sessions"),
                _optional(data, "task_github_identity", "sessions"),
            )
        )
    return found


def parse_defaults(answer: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """`(scope, scope_id, identity_id)` of every default (none without the field)."""
    found = []
    for item in wire.array(answer.get("github_defaults", []), "github_defaults"):
        data = wire.obj(item, "github_defaults")
        scope = data.get("scope")
        if scope not in SCOPES:
            raise wire.ResponseInvalid("github_defaults")
        found.append(
            (
                str(scope),
                wire.uuid_text(data.get("scope_id"), "github_defaults"),
                wire.uuid_text(data.get("identity_id"), "github_defaults"),
            )
        )
    return found


async def store(conn: AsyncConnection[Any], answer: Mapping[str, Any]) -> None:
    """Replace the stored choices and defaults with the answer's (in the caller's transaction)."""
    sessions, found = parse_sessions(answer), parse_defaults(answer)
    await conn.execute(
        "DELETE FROM runner.github_selection WHERE NOT (session_id = ANY(%s::uuid[]))", ([row[0] for row in sessions],)
    )
    for row in sessions:
        await conn.execute(
            "INSERT INTO runner.github_selection (session_id, task_id, github_identity, task_github_identity)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET task_id = EXCLUDED.task_id,"
            " github_identity = EXCLUDED.github_identity, task_github_identity = EXCLUDED.task_github_identity",
            row,
        )
    await conn.execute("DELETE FROM runner.github_default")
    for scope, scope_id, identity_id in found:
        await conn.execute(
            "INSERT INTO runner.github_default (scope, scope_id, identity_id) VALUES (%s, %s, %s)"
            " ON CONFLICT (scope, scope_id) DO UPDATE SET identity_id = EXCLUDED.identity_id",
            (scope, scope_id, identity_id),
        )


async def _task_choice(conn: AsyncConnection[Any], task_id: str) -> str | None:
    cur = await conn.execute(
        "SELECT task_github_identity::text FROM runner.github_selection WHERE task_id = %s"
        " ORDER BY task_github_identity IS NULL, session_id LIMIT 1",
        (task_id,),
    )
    other = await cur.fetchone()
    return None if other is None else other[0]


async def _child_choice(conn: AsyncConnection[Any], session: Any) -> tuple[bool, str | None]:
    """`(known, identity)` of a child session not listed yet: the `github_identity` of the `subagent.request` that
    started it, else its parent's session identity (the hub gives a child its parent's at creation); unknown while
    neither the request nor the parent's row is known here."""
    cur = await conn.execute(
        "SELECT registration->>'github_identity' FROM runner.operation WHERE child_session_id = %s"
        " AND type = 'subagent.request' AND registration IS NOT NULL ORDER BY created_at, id LIMIT 1",
        (session.id,),
    )
    row = await cur.fetchone()
    if row is None:
        return False, None
    if row[0] is not None:
        return True, str(row[0])
    cur = await conn.execute(
        "SELECT github_identity::text FROM runner.github_selection WHERE session_id = %s", (session.parent_id,)
    )
    parent = await cur.fetchone()
    return (False, None) if parent is None else (True, parent[0])


async def choices(conn: AsyncConnection[Any], session: Any) -> Choices | None:
    """The session's and its task's identity. A session the stored answer does not list yet: a main session takes the
    task's identity from another session of the task; a subagent session takes the identity of the request that
    started it (`_child_choice`), and is unknown (None: the operation waits for the next desired state) while that is
    not known - never the task's identity in place of its own."""
    cur = await conn.execute(
        "SELECT github_identity::text, task_github_identity::text FROM runner.github_selection WHERE session_id = %s",
        (session.id,),
    )
    row = await cur.fetchone()
    if row is not None:
        return Choices(row[0], row[1])
    task = await _task_choice(conn, session.task_id)
    if session.depth == 0:
        return Choices(None, task)
    known, own = await _child_choice(conn, session)
    return Choices(own, task) if known else None


async def defaults(conn: AsyncConnection[Any], org_id: str, wg_id: str) -> tuple[str | None, str | None]:
    """`(workgroup default, organization default)`."""
    cur = await conn.execute(
        "SELECT scope, identity_id::text FROM runner.github_default"
        " WHERE (scope = 'wg' AND scope_id = %s) OR (scope = 'org' AND scope_id = %s)",
        (wg_id, org_id),
    )
    found = {str(scope): str(identity) for scope, identity in await cur.fetchall()}
    return found.get("wg"), found.get("org")
