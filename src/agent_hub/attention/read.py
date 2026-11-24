"""Reading the attention records of an org (03-api.md section 4.6, ADR-076 sections 4 and 6).

Only tasks of the workgroups the caller reads appear (the rule of `wg:read`, `agent_hub.orgs.rights.wg_access`: an
active member with an unrevoked access; archived workgroups stay readable) - in the lists and in every counter.

- `list_attention`: the records of one category with their task rows, at most `limit` (default 50, max 100), ordered
  by the category's list order (`agent_hub.attention.derive.CATEGORIES`): `since ASC, task_id ASC` for
  `needs_decision` (longest waiting first), `since DESC, task_id DESC` otherwise. Filters: `mine` (tasks the caller
  owns) and `since` (records that entered their category at or after it). The cursor `next_before` has the form of
  the task lists (`agent_hub.device.tasks.encode_cursor`: the `since` of the page's last row and its task ID) and
  continues in the list order.
- `summary`: `needs_decision`, `needs_decision_mine`, `blocked`, `working` and `done_since` (the `done` records with
  `since` at or after the parameter, default the last 24 hours).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.attention.derive import ASCENDING, BLOCKED, CATEGORIES, DONE, NEEDS_DECISION, WORKING
from agent_hub.attention.store import ROW_COLUMNS, ROW_FIELDS, attention_json
from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.sessions import rows

Connection = psycopg.Connection[Any]
LIST_DEFAULT = 50
LIST_MAX = 100
DONE_WINDOW = timedelta(hours=24)
_ROW_WIDTH = len(ROW_FIELDS)
"""The attention columns come first, so a wider task row (more task columns of other packages) cannot shift them."""


def readable_workgroups(connection: Connection, org: UUID, user: UUID) -> list[UUID]:
    """The workgroups of the org the user reads (module docstring)."""
    found = connection.execute(
        "SELECT a.wg_id FROM hub.wg_access a JOIN hub.wg w ON w.id = a.wg_id"
        " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = a.user_id AND m.removed_seq IS NULL"
        " WHERE w.org_id = %s AND a.user_id = %s AND a.revoked_seq IS NULL ORDER BY a.wg_id",
        (org, user),
    ).fetchall()
    return [row[0] for row in found]


def list_attention(
    connection: Connection,
    org: UUID,
    user: UUID,
    *,
    category: str,
    mine: bool,
    since: datetime | None,
    before: str | None,
    limit: int,
) -> dict[str, Any]:
    """One category of the org's records with their task rows (module docstring); the caller checked `org:member`."""
    ascending = CATEGORIES[category][0] == ASCENDING
    clauses = ["a.org_id = %s", "a.category = %s", "a.wg_id = ANY(%s)"]
    params: list[Any] = [org, category, readable_workgroups(connection, org, user)]
    if mine:
        clauses.append("a.owner_id = %s")
        params.append(user)
    if since is not None:
        clauses.append("a.since >= %s")
        params.append(since)
    if before is not None:
        at, task = decode_cursor(before)
        clauses.append(f"(a.since, a.task_id) {'>' if ascending else '<'} (%s, %s)")
        params.extend([at, task])
    params.append(limit + 1)
    direction = "ASC" if ascending else "DESC"
    found = connection.execute(
        f"SELECT {ROW_COLUMNS}, {rows.TASK_COLUMNS} FROM hub.task_attention a JOIN hub.task t ON t.id = a.task_id"
        f" WHERE {' AND '.join(clauses)} ORDER BY a.since {direction}, a.task_id {direction} LIMIT %s",
        params,
    ).fetchall()
    page = found[:limit]
    items = [
        {"task": rows.task_row_json(row[_ROW_WIDTH:]), "attention": attention_json(row[:_ROW_WIDTH])} for row in page
    ]
    more = len(found) > limit  # then the page is full (limit >= 1)
    last = page[-1][:_ROW_WIDTH] if more else None
    return {"items": items, "next_before": None if last is None else encode_cursor(last[5], last[0])}


def summary(connection: Connection, org: UUID, user: UUID, since: datetime | None, now: datetime) -> dict[str, int]:
    """The counters over the readable workgroups (module docstring); the caller checked `org:member`."""
    done_after = now - DONE_WINDOW if since is None else since
    row = connection.execute(
        "SELECT count(*) FILTER (WHERE category = %(nd)s), count(*) FILTER (WHERE category = %(nd)s"
        " AND owner_id = %(user)s), count(*) FILTER (WHERE category = %(blocked)s),"
        " count(*) FILTER (WHERE category = %(working)s), count(*) FILTER (WHERE category = %(done)s)"
        " FROM hub.task_attention WHERE org_id = %(org)s AND wg_id = ANY(%(wgs)s)"
        " AND (category <> %(done)s OR since >= %(after)s)",
        {
            "nd": NEEDS_DECISION,
            "blocked": BLOCKED,
            "working": WORKING,
            "done": DONE,
            "user": user,
            "org": org,
            "wgs": readable_workgroups(connection, org, user),
            "after": done_after,
        },
    ).fetchone()
    names = ("needs_decision", "needs_decision_mine", "blocked", "working", "done_since")
    return dict(zip(names, (int(value) for value in cast(tuple[int, ...], row)), strict=True))
