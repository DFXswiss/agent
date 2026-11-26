"""Reading the open main agents of an org by person (03-api.md section 4.6, ADR-095 section 5).

A main agent is open while its task is not `closed`; its responsible person is the task's owner. Only tasks of the
workgroups the caller reads count (the rule of the situation view, `agent_hub.attention.read.readable_workgroups`) - in
the rows and in every counter. Both reads use the partial index `task_open_owner` (`org_id`, `owner_id`,
`last_activity_at DESC`, `id DESC`) of the open tasks.

- `people`: one row per owner with open main agents - `agents` (their number), `needs_decision`, `blocked` and
  `working` (by the category of the task's attention record; a task without a record counts in none of them) and
  `subagents_active` (the sum of the records' `subagents_active`). The order is the caller's row first, then the
  others by `owner_id`; `after` continues after that owner in this order (after the caller's own row: the first of the
  others). At most `limit` rows (default 50, max 100); `next_after` is the owner of the page's last row while more
  rows follow.
- `main_agents`: the open main agents of one owner with their attention record (null while the hub has none), newest
  activity first (`last_activity_at DESC`, `task_id DESC`), at most `limit` (default 50, max 100), with the cursor
  `next_before` of the task lists (`agent_hub.device.tasks.encode_cursor`: the activity time of the page's last row and
  its task ID).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.attention.derive import BLOCKED, NEEDS_DECISION, WORKING
from agent_hub.attention.read import readable_workgroups
from agent_hub.attention.store import ROW_COLUMNS, ROW_FIELDS, attention_json
from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.sessions import rows

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 100
PERSON_FIELDS = ("owner_id", "agents", "needs_decision", "blocked", "working", "subagents_active")
_ROW_WIDTH = len(ROW_FIELDS)
_ACTIVITY_INDEX = 8
"""The index of `last_activity_at` in `rows.TASK_COLUMNS` (the cursor's time)."""
OWN = "t.owner_id = %(user)s"
"""The owners of the people query: the caller's own row."""
OTHERS = "t.owner_id <> %(user)s"
"""The others from the first of them (first page, or after the caller's row)."""
OTHERS_AFTER = "t.owner_id <> %(user)s AND t.owner_id > %(after)s"
"""The others after the owner of a cursor."""
_PEOPLE = (
    "SELECT t.owner_id, count(*), count(*) FILTER (WHERE a.category = %(nd)s),"
    " count(*) FILTER (WHERE a.category = %(blocked)s), count(*) FILTER (WHERE a.category = %(working)s),"
    " coalesce(sum(a.subagents_active), 0)"
    " FROM hub.task t LEFT JOIN hub.task_attention a ON a.task_id = t.id"
    " WHERE t.org_id = %(org)s AND t.state <> 'closed' AND t.wg_id = ANY(%(wgs)s) AND {owners}"
    " GROUP BY t.owner_id ORDER BY t.owner_id LIMIT %(limit)s"
)


def people_statement(owners: str) -> str:
    """The aggregate of the people query for one of the owner conditions `OWN`, `OTHERS`, `OTHERS_AFTER`."""
    return _PEOPLE.format(owners=owners)


def people_values(connection: Connection, org: UUID, user: UUID, after: UUID | None) -> dict[str, Any]:
    """The parameters of the people query except `limit`."""
    return {
        "nd": NEEDS_DECISION,
        "blocked": BLOCKED,
        "working": WORKING,
        "org": org,
        "wgs": readable_workgroups(connection, org, user),
        "user": user,
        "after": after,
    }


def _person(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        name: str(value) if name == "owner_id" else int(value) for name, value in zip(PERSON_FIELDS, row, strict=True)
    }


def people(connection: Connection, org: UUID, user: UUID, *, after: UUID | None, limit: int) -> dict[str, Any]:
    """The people of the org with open main agents (module docstring); the caller checked `org:member`."""
    values = people_values(connection, org, user, after)
    page: list[dict[str, Any]] = []
    if after is None:
        own = connection.execute(people_statement(OWN), {**values, "limit": 1}).fetchall()
        page.extend(_person(row) for row in own)
    others = limit - len(page)
    condition = OTHERS if after in (None, user) else OTHERS_AFTER
    found = connection.execute(people_statement(condition), {**values, "limit": others + 1}).fetchall()
    page.extend(_person(row) for row in found[:others])
    more = len(found) > others  # then the page is full (limit >= 1)
    return {"people": page, "next_after": page[-1]["owner_id"] if more else None}


def main_agents_query(
    connection: Connection, org: UUID, user: UUID, *, owner: UUID, before: str | None, limit: int
) -> tuple[str, list[Any]]:
    """The statement of one page of `main_agents` (one row more than `limit`) and its parameters."""
    clauses = ["t.org_id = %s", "t.owner_id = %s", "t.state <> 'closed'", "t.wg_id = ANY(%s)"]
    params: list[Any] = [org, owner, readable_workgroups(connection, org, user)]
    if before is not None:
        at, task = decode_cursor(before)
        clauses.append("(t.last_activity_at, t.id) < (%s, %s)")
        params.extend([at, task])
    params.append(limit + 1)
    statement = (
        f"SELECT {ROW_COLUMNS}, {rows.TASK_COLUMNS} FROM hub.task t LEFT JOIN hub.task_attention a ON a.task_id = t.id"
        f" WHERE {' AND '.join(clauses)} ORDER BY t.last_activity_at DESC, t.id DESC LIMIT %s"
    )
    return statement, params


def main_agents(
    connection: Connection, org: UUID, user: UUID, *, owner: UUID, before: str | None, limit: int
) -> dict[str, Any]:
    """The open main agents of `owner` with their attention records (module docstring); the caller checked
    `org:member`."""
    statement, params = main_agents_query(connection, org, user, owner=owner, before=before, limit=limit)
    found = connection.execute(statement, params).fetchall()
    page = found[:limit]
    items = [
        {
            "task": rows.task_row_json(row[_ROW_WIDTH:]),
            "attention": None if row[0] is None else attention_json(row[:_ROW_WIDTH]),
        }
        for row in page
    ]
    more = len(found) > limit  # then the page is full (limit >= 1)
    last = page[-1][_ROW_WIDTH:] if more else None
    return {"items": items, "next_before": None if last is None else encode_cursor(last[_ACTIVITY_INDEX], last[0])}
