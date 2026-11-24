"""Routes of the billing area (12-billing.md section 6, ADR-062 section 7).

- `GET /v1/orgs/{org}/billing` (`org:member`): balance, exhausted, the price list in force, the credit history and the
  consumption per day and per workgroup of the last 30 days (a share without workgroup - a per-device DFX key's
  spend no session reported, ADR-080 section 7 - counts in the days, not in the workgroups);
- `GET /v1/workgroups/{wg}/charges` (`wg:read`): charges per task of the last 30 days;
- `GET /v1/tasks/{task}/charges` (`task:read`): charges per session of the task.

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
from agent_hub.billing.outbox import NO_WG
from agent_hub.crypto.encoding import format_time
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.usage.budget import utc_day
from agent_hub.workgroups.routes import path_id

router = HubRouter()
DAYS = 30
CREDITS = 100
TASKS = 200
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
        (org, CREDITS),
    ).fetchall()
    days = connection.execute(
        f"SELECT day, {_SUMS} FROM hub.charge_daily WHERE org_id = %s AND day >= %s GROUP BY day ORDER BY day",
        (org, first),
    ).fetchall()
    wgs = connection.execute(
        f"SELECT wg_id, {_SUMS} FROM hub.charge_daily WHERE org_id = %s AND day >= %s AND wg_id <> %s GROUP BY wg_id"
        " ORDER BY wg_id",
        (org, first, NO_WG),
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
    }


def wg_charges(connection: Any, wg: UUID, now: datetime) -> dict[str, Any]:
    """The answer of `GET /v1/workgroups/{wg}/charges` (module docstring)."""
    rows = connection.execute(
        f"SELECT task_id, {_CHARGE_SUMS} FROM hub.charge WHERE wg_id = %s AND at >= %s GROUP BY task_id"
        " ORDER BY sum(amount_micro_usd) DESC, task_id LIMIT %s",
        (wg, since(now), TASKS),
    ).fetchall()
    total = connection.execute(
        "SELECT COALESCE(sum(amount_micro_usd), 0) FROM hub.charge_daily WHERE wg_id = %s AND day >= %s",
        (wg, utc_day(since(now))),
    ).fetchone()
    return {
        "tasks": [{"task_id": str(task), **sums(row)} for task, *row in rows],
        "total_micro_usd": int(cast(tuple[int], total)[0]),
    }


def task_charges(connection: Any, task: UUID) -> dict[str, Any]:
    """The answer of `GET /v1/tasks/{task}/charges` (module docstring)."""
    rows = connection.execute(
        f"SELECT session_id, {_CHARGE_SUMS} FROM hub.charge WHERE task_id = %s GROUP BY session_id"
        " ORDER BY min(at), session_id",
        (task,),
    ).fetchall()
    totals = [(str(session), sums(row)) for session, *row in rows]
    sessions = [{"session_id": session, **found} for session, found in totals]
    total = sum(found["tokens_micro_usd"] + found["runtime_micro_usd"] for _session, found in totals)
    return {"sessions": sessions, "total_micro_usd": total}


def _org(context: HubContext, user: UUID, org: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        return org_billing(connection, org, context.clock())


def _wg(context: HubContext, user: UUID, wg: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        return wg_charges(connection, wg, context.clock())


def _task(context: HubContext, user: UUID, task: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return task_charges(connection, task)


@router.get("/v1/orgs/{org}/billing", auth="browser", right="org:member", read_resource="org.billing")
async def get_org_billing(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    binding.bind(context.config)
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_org, context, user, path_id(org))


@router.get("/v1/workgroups/{wg}/charges", auth="browser", right="wg:read", read_resource="workgroup.charges")
async def get_wg_charges(context: Context, principal: CurrentPrincipal, wg: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_wg, context, user, path_id(wg))


@router.get("/v1/tasks/{task}/charges", auth="browser", right="task:read", read_resource="task.charges")
async def get_task_charges(context: Context, principal: CurrentPrincipal, task: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_task, context, user, path_id(task))
