"""Routes of the notifications area (03-api.md section 4.9).

| Route | Right |
|---|---|
| `GET /v1/notifications?before&limit` | `user`: the caller's own notifications |
| `POST /v1/notifications/read` | `user`: `{ids:[…]}` (1-200) or `{all:true}` → `{}` |

The list is newest first (`created_at DESC, id DESC`, at most `limit` rows, default 50, max 200) with the opaque cursor
`next_before` of the other lists (`agent_hub.device.tasks.encode_cursor`). Marking read sets `read_at` (the hub clock)
on the caller's unread rows among `ids` (or on all of them); IDs that are unknown, already read or of another user are
ignored, so the answer never tells whether a foreign ID exists. The unread count is part of `GET /v1/me`
(`unread_notifications`). The read log `read.notifications` is written by the pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import Field

from agent_hub.auth.fields import Strict, Uuid7
from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.notifications.rules import COLUMNS, row

router = HubRouter()
LIST_DEFAULT = 50
LIST_MAX = 200


class ReadIds(Strict):
    """`{ids:[…]}`."""

    ids: list[Uuid7] = Field(min_length=1, max_length=LIST_MAX)


class ReadAll(Strict):
    """`{all:true}`."""

    all: Literal[True]


def list_notifications(connection: Any, user: UUID, *, before: str | None, limit: int) -> dict[str, Any]:
    """A page of the user's notifications, newest first (module docstring)."""
    clauses, params = ["recipient_id = %s"], list[Any]([user])
    if before is not None:
        at, last = decode_cursor(before)
        clauses.append("(created_at, id) < (%s, %s)")
        params.extend([at, last])
    found = connection.execute(
        f"SELECT {COLUMNS} FROM hub.notification WHERE {' AND '.join(clauses)}"
        " ORDER BY created_at DESC, id DESC LIMIT %s",
        (*params, limit + 1),
    ).fetchall()
    page = [row(item) for item in found[:limit]]
    return {
        "notifications": [item.to_json() for item in page],
        "next_before": encode_cursor(page[-1].created_at, page[-1].id) if len(found) > limit else None,
    }


def _list(context: HubContext, user: UUID, before: str | None, limit: int) -> dict[str, Any]:
    with context.pool.connection() as connection:
        return list_notifications(connection, user, before=before, limit=limit)


@router.get("/v1/notifications", auth="browser", right="user", read_resource="notifications")
async def get_notifications(
    context: Context, principal: CurrentPrincipal, before: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    count = parse_count(limit, LIST_DEFAULT, 1, LIST_MAX)
    return await asyncio.to_thread(_list, context, cast(UUID, principal.principal_id), before, count)


def mark_read(connection: Any, user: UUID, ids: list[str] | None, now: Any) -> int:
    """Set `read_at` on the user's unread rows among `ids` (all of them for None); the number of rows marked."""
    clauses, params = ["recipient_id = %s", "read_at IS NULL"], list[Any]([now, user])
    if ids is not None:
        clauses.append("id = ANY(%s::uuid[])")
        params.append(sorted(set(ids)))
    marked = connection.execute(f"UPDATE hub.notification SET read_at = %s WHERE {' AND '.join(clauses)}", params)
    return int(marked.rowcount)


def _read(context: HubContext, user: UUID, ids: list[str] | None) -> None:
    now = context.clock()
    with context.pool.connection() as connection, connection.transaction():
        mark_read(connection, user, ids, now)


@router.post("/v1/notifications/read", auth="browser", right="user")
async def post_read(context: Context, principal: CurrentPrincipal, body: ReadIds | ReadAll) -> dict[str, Any]:
    ids = list(body.ids) if isinstance(body, ReadIds) else None
    await asyncio.to_thread(_read, context, cast(UUID, principal.principal_id), ids)
    return {}
