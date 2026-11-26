"""The agent as the actor of an audit row (ADR-095 section 4, KOR-52; 02-data-model.md section 2.9).

The hub chooses the actor kind from the session's `depth`, never from what a device says: a main session (depth 0)
acts as `main_agent`, a child session as `subagent`; `actor_id` is the session and `device_id` the device that
reported the row. The closed rule of the rows written this way (operation steps, the start of a child, outbox `audit`
items that name a session) lives with their writers; this module only names the kind, reads a session's actor and
answers the `actors` map of an audit page: for every `main_agent`/`subagent` actor its task, the task's number, its
`agent_path` and its parent session (`contracts/api/objects.json#/$defs/audit_agent`), read in one query.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import psycopg

Connection = psycopg.Connection[Any]
MAIN_AGENT = "main_agent"
SUBAGENT = "subagent"
AGENT_KINDS = frozenset({MAIN_AGENT, SUBAGENT})


@dataclass(frozen=True)
class SessionActor:
    """The audit actor of a session: its kind, the session and the person it acts for (its `triggered_by`)."""

    kind: str
    id: UUID
    on_behalf_of: UUID | None


def agent_kind(depth: int) -> str:
    """`main_agent` for a main session (depth 0), else `subagent`."""
    return MAIN_AGENT if depth == 0 else SUBAGENT


def session_actor(connection: Connection, session_id: UUID) -> SessionActor:
    """The actor of an existing session; `on_behalf_of` is the session's `triggered_by` (ADR-083 section 2) while it
    is an active member of the session's org (admin or member), else nobody."""
    depth, triggered_by = cast(
        tuple[int, UUID | None],
        connection.execute(
            "SELECT s.depth, m.principal_id FROM hub.session s LEFT JOIN hub.org_member m ON m.org_id = s.org_id"
            " AND m.principal_id = s.triggered_by AND m.removed_seq IS NULL AND m.role IN ('admin', 'member')"
            " WHERE s.id = %s",
            (session_id,),
        ).fetchone(),
    )
    return SessionActor(agent_kind(depth), session_id, triggered_by)


def actors(connection: Connection, rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The `actors` map of an audit page (module docstring): one entry per agent actor of `rows` (API rows)."""
    sessions = sorted({row["actor_id"] for row in rows if row["actor_kind"] in AGENT_KINDS})
    if not sessions:
        return {}
    found = connection.execute(
        "SELECT s.id, s.task_id, t.number, s.agent_path, s.parent_session_id FROM hub.session s"
        " JOIN hub.task t ON t.id = s.task_id WHERE s.id = ANY(%s::uuid[])",
        (sessions,),
    ).fetchall()
    return {
        str(session): {
            "task_id": str(task),
            "number": int(number),
            "agent_path": path,
            "parent_session_id": None if parent is None else str(parent),
        }
        for session, task, number, path, parent in found
    }
