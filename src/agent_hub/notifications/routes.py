"""Routes of the notifications area (03-api.md section 4.9).

| Route | Right |
|---|---|
| `GET /v1/notifications?before&limit&account` | `user`: the caller's own notifications |
| `POST /v1/notifications/read` | `user`: `{ids:[…]}` (1-200) or `{all:true, account?}` → `{}` |

Account notifications (`agent.account_change`, no org; KOR-24) are listed only with `account=1` and marked read by
`{all:true}` only with `account:true`, so a web client of before KOR-24 never sees them (decisions KOR-24); marking
by ID works for every row. Marking read by an agent is audited (`notification.read {marked, scope}`).

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

from agent_hub.audit.chain import ACTING_AGENT, AuditRow, append_audit
from agent_hub.auth.fields import Strict, Uuid7
from agent_hub.device.tasks import decode_cursor, encode_cursor
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.notifications.rules import COLUMNS, row

router = HubRouter()
LIST_DEFAULT = 50
LIST_MAX = 200
READ_ACTION = "notification.read"


class ReadIds(Strict):
    """`{ids:[…]}`."""

    ids: list[Uuid7] = Field(min_length=1, max_length=LIST_MAX)


class ReadAll(Strict):
    """`{all:true}` (with `account:true` also the account notifications)."""

    all: Literal[True]
    account: bool = False


def list_notifications(
    connection: Any, user: UUID, *, before: str | None, limit: int, account: bool = False
) -> dict[str, Any]:
    """A page of the user's notifications, newest first (module docstring); account notifications only with
    `account`."""
    clauses, params = ["recipient_id = %s"], list[Any]([user])
    if not account:
        clauses.append("org_id IS NOT NULL")
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


def _list(context: HubContext, user: UUID, before: str | None, limit: int, account: bool) -> dict[str, Any]:
    with context.pool.connection() as connection:
        return list_notifications(connection, user, before=before, limit=limit, account=account)


@router.get("/v1/notifications", auth="browser", right="user", read_resource="notifications")
async def get_notifications(
    context: Context,
    principal: CurrentPrincipal,
    before: str | None = None,
    limit: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    count = parse_count(limit, LIST_DEFAULT, 1, LIST_MAX)
    if account not in (None, "1"):
        raise HubError("request_invalid", detail={"pointers": ["/account"]})
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_list, context, user, before, count, account == "1")


def mark_read(connection: Any, user: UUID, ids: list[str] | None, now: Any, *, account: bool = False) -> int:
    """Set `read_at` on the user's unread rows among `ids` (all of them for None: the account notifications only with
    `account`); the number of rows marked."""
    clauses, params = ["recipient_id = %s", "read_at IS NULL"], list[Any]([now, user])
    if ids is not None:
        clauses.append("id = ANY(%s::uuid[])")
        params.append(sorted(set(ids)))
    elif not account:
        clauses.append("org_id IS NOT NULL")
    marked = connection.execute(f"UPDATE hub.notification SET read_at = %s WHERE {' AND '.join(clauses)}", params)
    return int(marked.rowcount)


def _scope(ids: list[str] | None, account: bool) -> str:
    if ids is not None:
        return "ids"
    return "all_account" if account else "all"


def _read(context: HubContext, user: UUID, ids: list[str] | None, account: bool) -> None:
    now = context.clock()
    with context.pool.connection() as connection, connection.transaction():
        marked = mark_read(connection, user, ids, now, account=account)
        if ACTING_AGENT.get() is not None:
            # an agent marked notifications read (it could hide an account notification): audited (KOR-24)
            append_audit(
                connection,
                AuditRow(
                    at=now,
                    actor_kind="user",
                    actor_id=user,
                    action=READ_ACTION,
                    result="ok",
                    detail={"marked": marked, "scope": _scope(ids, account)},
                ),
            )


@router.post("/v1/notifications/read", auth="browser", right="user")
async def post_read(context: Context, principal: CurrentPrincipal, body: ReadIds | ReadAll) -> dict[str, Any]:
    ids = list(body.ids) if isinstance(body, ReadIds) else None
    account = isinstance(body, ReadAll) and body.account
    await asyncio.to_thread(_read, context, cast(UUID, principal.principal_id), ids, account)
    return {}
