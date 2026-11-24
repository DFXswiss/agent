"""Routes of the billing area (12-billing.md section 6, ADR-062 section 7).

- `GET /v1/orgs/{org}/billing` (`org:member`): balance, exhausted, the price list in force, the credit history (the
  newest `CREDITS` entries, `credits_truncated: true` when older ones exist) and the consumption per day, per workgroup
  and per person (`people`, `hub.charge_person_daily`) of the last 30 days (a share
  without workgroup - a per-device DFX key's spend no session reported, ADR-081 section 7 - counts in the days and as
  unattributed consumption, not in the workgroups);
- `GET /v1/workgroups/{wg}/charges` (`wg:read`): charges per task of the last 30 days;
- `GET /v1/tasks/{task}/charges` (`task:read`): charges per session and per person (`people`) of the task.

`people` (KOR-46, ADR-083 section 2) is the consumption per person whose instruction caused it (the consumption item's
`triggered_by`), largest amount first; unattributed consumption is one entry with `user_id` null. Charges stay booked to
the organization: the breakdown changes neither the balance nor a booking.

The two charge lists are bounded (ADR-086 section 2, `agent_hub.events.paging`): without `before`/`limit` they answer
as before (the workgroup's tasks by total descending, the task's sessions by their first charge) cut at their cap
(`TASKS`, `SESSIONS`) with `truncated: true` when cut; with `before` and/or `limit` they answer one page (default 50,
max 200) newest first by the UUIDv7 ID of the task or session (its creation order, immutable while charges arrive,
so a page is stable) and `next_before`. The cursor is the opaque paging cursor with the ID as key and time 0. The
total is always that of the whole workgroup (30 days) or task.

Amounts are micro-USD. Unknown, malformed and foreign IDs answer `404 not_found` alike; the reads are read-logged by
the pipeline (`read.org.billing`, `read.workgroup.charges`, `read.task.charges`). Nothing here changes credit: there
is no payment method and no top-up in the interface (the operator sets credit, `agent_hub.billing.cli`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from agent_hub.billing import binding, credit, prices
from agent_hub.billing.outbox import NO_WG, UNATTRIBUTED
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.events import paging
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.usage.budget import utc_day
from agent_hub.workgroups.routes import path_id

router = HubRouter()
DAYS = 30
CREDITS = 100
TASKS = 200
SESSIONS = paging.CAP_LARGE
_SUMS = (
    "COALESCE(sum(amount_micro_usd) FILTER (WHERE kind = 'tokens'), 0),"
    " COALESCE(sum(amount_micro_usd) FILTER (WHERE kind = 'runtime'), 0),"
    " COALESCE(sum(quantity) FILTER (WHERE kind = 'tokens'), 0),"
    " COALESCE(sum(quantity) FILTER (WHERE kind = 'runtime'), 0)"
)
_CHARGE_SUMS = (
    "COALESCE(sum(amount_micro_usd) FILTER (WHERE kind = 'tokens'), 0),"
    " COALESCE(sum(amount_micro_usd) FILTER (WHERE kind = 'runtime'), 0),"
    " COALESCE(sum((quantity->>'input_tokens')::bigint + (quantity->>'output_tokens')::bigint"
    " + (quantity->>'cache_read_tokens')::bigint + (quantity->>'cache_write_tokens')::bigint)"
    " FILTER (WHERE kind = 'tokens'), 0),"
    " COALESCE(sum((quantity->>'runtime_ms')::bigint) FILTER (WHERE kind = 'runtime'), 0)"
)


def sums(row: tuple[Any, ...]) -> dict[str, int]:
    tokens_usd, runtime_usd, tokens, runtime_ms = (int(value) for value in row)
    return {
        "tokens_micro_usd": tokens_usd,
        "runtime_micro_usd": runtime_usd,
        "tokens": tokens,
        "runtime_ms": runtime_ms,
    }


def person(user: UUID | None) -> str | None:
    """The `user_id` of a `charge_person` entry: null for unattributed consumption (`UNATTRIBUTED` or NULL)."""
    return None if user is None or user == UNATTRIBUTED else str(user)


def people(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """`charge_person` entries of `(user, *sums)` rows, largest amount first (then by user)."""
    found = [{"user_id": person(row[0]), **sums(row[1:])} for row in rows]
    return sorted(
        found, key=lambda item: (-(item["tokens_micro_usd"] + item["runtime_micro_usd"]), item["user_id"] or "")
    )


def since(now: datetime) -> datetime:
    """The first moment of the window of the last `DAYS` UTC days (today included)."""
    return datetime.combine(utc_day(now) - timedelta(days=DAYS - 1), datetime.min.time(), now.tzinfo)


def org_billing(connection: Any, org: UUID, now: datetime) -> dict[str, Any]:
    """The answer of `GET /v1/orgs/{org}/billing` (module docstring)."""
    version = prices.in_force(connection, now)
    first = utc_day(since(now))
    credits = connection.execute(
        "SELECT at, kind, amount_micro_usd, balance_micro_usd, reason, actor FROM hub.credit_entry WHERE org_id = %s"
        " ORDER BY n DESC LIMIT %s",
        (org, CREDITS + 1),
    ).fetchall()
    credits, cut = paging.cut(credits, CREDITS)
    days = connection.execute(
        f"SELECT day, {_SUMS} FROM hub.charge_daily WHERE org_id = %s AND day >= %s GROUP BY day ORDER BY day",
        (org, first),
    ).fetchall()
    wgs = connection.execute(
        f"SELECT wg_id, {_SUMS} FROM hub.charge_daily WHERE org_id = %s AND day >= %s AND wg_id <> %s GROUP BY wg_id"
        " ORDER BY wg_id",
        (org, first, NO_WG),
    ).fetchall()
    persons = connection.execute(
        f"SELECT user_id, {_SUMS} FROM hub.charge_person_daily WHERE org_id = %s AND day >= %s GROUP BY user_id",
        (org, first),
    ).fetchall()
    balance = credit.balance(connection, org)
    return {
        "currency": prices.CURRENCY,
        "balance_micro_usd": balance,
        "exhausted": version is not None and balance <= 0,
        "billing_active": version is not None,
        "price_list": None
        if version is None
        else {
            "version": version.version,
            "valid_from": format_time(version.valid_from),
            "body": version.prices.document,
        },
        "credits": [
            {
                "at": format_time(at),
                "kind": kind,
                "amount_micro_usd": int(amount),
                "balance_micro_usd": int(after),
                "reason": reason,
                "actor": actor,
            }
            for at, kind, amount, after, reason, actor in credits
        ],
        "days": [{"day": day.isoformat(), **sums(row)} for day, *row in days],
        "workgroups": [{"wg_id": str(wg), **sums(row)} for wg, *row in wgs],
        "people": people(persons),
        **({"credits_truncated": True} if cut else {}),
    }


def _keyset(window: paging.Window, column: str, cap_order: str) -> tuple[str, str, dict[str, Any]]:
    """`(extra condition, order, parameters)` of a charge list read in `window` (module docstring)."""
    if not window.paged:
        return "", cap_order, {}
    if window.before is None:
        return "", f"{column} DESC", {}
    return f" AND {column} < %(before)s", f"{column} DESC", {"before": window.before[1]}


def _last(rows: list[Any], more: bool) -> tuple[datetime, str] | None:
    """The cursor key of the last row when more rows follow (its ID, time 0)."""
    return (paging.EPOCH, str(rows[-1][0])) if more else None


def wg_charges(connection: Any, wg: UUID, now: datetime, window: paging.Window) -> dict[str, Any]:
    """The answer of `GET /v1/workgroups/{wg}/charges` (module docstring)."""
    extra, order, params = _keyset(window, "task_id", "sum(amount_micro_usd) DESC, task_id")
    rows, more = paging.cut(
        connection.execute(
            f"SELECT task_id, {_CHARGE_SUMS} FROM hub.charge WHERE wg_id = %(wg)s AND at >= %(since)s{extra}"
            f" GROUP BY task_id ORDER BY {order} LIMIT %(limit)s",
            {"wg": wg, "since": since(now), "limit": window.limit + 1, **params},
        ).fetchall(),
        window.limit,
    )
    total = connection.execute(
        "SELECT COALESCE(sum(amount_micro_usd), 0) FROM hub.charge_daily WHERE wg_id = %s AND day >= %s",
        (wg, utc_day(since(now))),
    ).fetchone()
    return {
        "tasks": [{"task_id": str(task), **sums(row)} for task, *row in rows],
        "total_micro_usd": int(cast(tuple[int], total)[0]),
        **window.extra(_last(rows, more)),
    }


def task_charges(connection: Any, task: UUID, window: paging.Window) -> dict[str, Any]:
    """The answer of `GET /v1/tasks/{task}/charges` (module docstring)."""
    extra, order, params = _keyset(window, "session_id", "min(at), session_id")
    rows, more = paging.cut(
        connection.execute(
            f"SELECT session_id, {_CHARGE_SUMS} FROM hub.charge WHERE task_id = %(task)s{extra} GROUP BY session_id"
            f" ORDER BY {order} LIMIT %(limit)s",
            {"task": task, "limit": window.limit + 1, **params},
        ).fetchall(),
        window.limit,
    )
    total = connection.execute(
        "SELECT COALESCE(sum(amount_micro_usd), 0) FROM hub.charge WHERE task_id = %s", (task,)
    ).fetchone()
    persons = connection.execute(
        f"SELECT triggered_by, {_CHARGE_SUMS} FROM hub.charge WHERE task_id = %s GROUP BY triggered_by", (task,)
    ).fetchall()
    return {
        "sessions": [{"session_id": str(session), **sums(row)} for session, *row in rows],
        "total_micro_usd": int(cast(tuple[int], total)[0]),
        "people": people(persons),
        **window.extra(_last(rows, more)),
    }


def _org(context: HubContext, user: UUID, org: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        return org_billing(connection, org, context.clock())


def _wg(context: HubContext, user: UUID, wg: UUID, window: paging.Window) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        return wg_charges(connection, wg, context.clock(), window)


def _task(context: HubContext, user: UUID, task: UUID, window: paging.Window) -> dict[str, Any]:
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return task_charges(connection, task, window)


@router.get("/v1/orgs/{org}/billing", auth="browser", right="org:member", read_resource="org.billing")
async def get_org_billing(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    binding.bind(context.config)
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_org, context, user, path_id(org))


@router.get("/v1/workgroups/{wg}/charges", auth="browser", right="wg:read", read_resource="workgroup.charges")
async def get_wg_charges(
    context: Context, principal: CurrentPrincipal, wg: str, before: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    wg_id = path_id(wg)
    window = paging.window(before, limit, TASKS, UUID)
    return await asyncio.to_thread(_wg, context, user, wg_id, window)


@router.get("/v1/tasks/{task}/charges", auth="browser", right="task:read", read_resource="task.charges")
async def get_task_charges(
    context: Context, principal: CurrentPrincipal, task: str, before: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    task_id = path_id(task)
    window = paging.window(before, limit, SESSIONS, UUID)
    return await asyncio.to_thread(_task, context, user, task_id, window)
