"""Notifications, quotas, monitoring and audit (11-agent-access.md section 8.4; twins of
`web/src/core/notifications/actions.ts`, `credentials/quotas.ts`, `store/slices/monitoring.ts`, `monitoring/series.ts`,
`store/slices/audit.ts`, `monitoring/audit.ts` and `agents/activity.ts`).

- `notifications list`: `GET /v1/notifications?account=1` newest first (with the account-level rows, as the web);
  the hub has no unread filter, so `--unread` filters here; `unread` is the count of `GET /v1/me`.
- `notifications read`: `POST /v1/notifications/read` with `{ids}` (at most 200) or `{all: true}`.
- `quotas`: `GET /v1/me/quotas`, the newest row per window (`five_hour`, `seven_day`, `weekly:<model>`), malformed rows
  left out, ordered five hours, week, then by model; no rows: not available. `unreadable_since` (KOR-40): since when
  the subscription refuses the read (a long-lived token of `claude setup-token` may not read quotas; sessions still
  run), else null.
- `monitoring --org`: `GET /v1/monitoring/overview?org=` (devices, VMs, sessions and their work VMs); with
  `--entity-kind/--entity-id/--metric` one series of a range of the web's charts (`GET /v1/monitoring/series`, the
  window aligned to the step like the web).
- `audit`: the account audit (`GET /v1/me/audit`) or with `--org` the org audit with the hub's filters
  (`GET /v1/orgs/{org}/audit`); `--verify` asks the hub to verify the org's audit chain
  (`GET /v1/orgs/{org}/audit/verify`, org admins). Malformed rows are left out.
"""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_runner.crypto.encoding import format_time

NOTIFICATIONS_PAGE = 50
READ_MAX = 200
AUDIT_PAGE = 100
WINDOW_RE = re.compile(r"five_hour|seven_day|weekly:[A-Za-z0-9._-]{1,64}")
ACTION_RE = re.compile(r"[a-z_]+(?:\.[a-z_]+)*")
ACTION_MAX = 96
RANGES = {
    "15m": (900, 15),
    "1h": (3600, 15),
    "6h": (21_600, 60),
    "24h": (86_400, 300),
    "7d": (604_800, 1800),
    "30d": (2_592_000, 7200),
    "90d": (7_776_000, 21_600),
}
METRICS = (
    "cpu_ratio",
    "mem_used_bytes",
    "mem_limit_bytes",
    "disk_used_bytes",
    "disk_limit_bytes",
    "net_rx_bytes",
    "net_tx_bytes",
)
ENTITY_KINDS = ("device", "vm", "session", "workvm")
ENTITY_ID_RES = {"vm": re.compile(r"vm-[0-9a-f]{8}"), "workvm": re.compile(r"wvm-[0-9a-f]{8}")}
AUDIT_NULLABLE = (
    "actor_id",
    "org_id",
    "wg_id",
    "task_id",
    "session_id",
    "device_id",
    "target_kind",
    "target_id",
    "event_id",
)


# --- notifications ---


def notifications_list(account: Account, unread: bool, before: str | None) -> dict[str, Any]:
    query = {"account": "1", "limit": NOTIFICATIONS_PAGE, **({"before": before} if before else {})}
    answer = account.call("GET", f"/v1/notifications?{urlencode(query)}")
    rows = [row for row in rows_of(answer, "notifications") if isinstance(row.get("notification_id"), str)]
    following = answer.get("next_before")
    count = account.me().get("unread_notifications")
    return {
        "notifications": [row for row in rows if not unread or row.get("read_at") is None],
        "next_before": following if isinstance(following, str) else None,
        "unread": count if isinstance(count, int) else None,
    }


def notifications_read(account: Account, ids: list[str], every: bool) -> dict[str, Any]:
    if every == bool(ids) or len(ids) > READ_MAX:
        raise PlatformError("invalid_argument")
    body: dict[str, Any] = {"all": True} if every else {"ids": [uuid_argument(item) for item in ids]}
    account.call("POST", "/v1/notifications/read", body)
    return {"read": "all" if every else len(ids)}


# --- quotas ---


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def quota_bar(value: dict[str, Any]) -> dict[str, Any] | None:
    """One row as a bar, or None when it is malformed."""
    window, used = value.get("window"), value.get("used_percent")
    if not isinstance(window, str) or not WINDOW_RE.fullmatch(window):
        return None
    if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used) or used < 0:
        return None
    fetched, resets = _time(value.get("fetched_at")), value.get("resets_at")
    if fetched is None or (resets is not None and _time(resets) is None):
        return None
    severity = value.get("severity")
    return {
        "window": window,
        "model": window.removeprefix("weekly:") if window.startswith("weekly:") else None,
        "used_percent": used,
        "resets_at": resets,
        "severity": severity if isinstance(severity, str) else None,
        "fetched_at": value["fetched_at"],
        "_fetched": fetched,
    }


def quotas(account: Account) -> dict[str, Any]:
    newest: dict[str, dict[str, Any]] = {}
    answer = account.call("GET", "/v1/me/quotas")
    unreadable = answer.get("unreadable")
    since = unreadable.get("since") if isinstance(unreadable, dict) else None
    for row in rows_of(answer, "quotas"):
        bar = quota_bar(row)
        if bar is None:
            continue
        known = newest.get(bar["window"])
        if known is None or known["_fetched"] < bar["_fetched"]:
            newest[bar["window"]] = bar

    def rank(bar: dict[str, Any]) -> tuple[int, str]:
        order = {"five_hour": 0, "seven_day": 1}
        return order.get(bar["window"], 2), bar["window"]

    bars = [{k: v for k, v in bar.items() if k != "_fetched"} for bar in sorted(newest.values(), key=rank)]
    return {"available": bool(bars), "quotas": bars, "unreadable_since": since if _time(since) is not None else None}


# --- monitoring ---


def series_window(range_id: str, at_ms: int) -> tuple[int, int, int]:
    """`(from, to, step)` of a range, ending with the bucket that holds `now` (ms, ms, s)."""
    seconds, step = RANGES[range_id]
    to = (at_ms // (step * 1000) + 1) * step * 1000
    return to - seconds * 1000, to, step


def now_ms() -> int:
    return int(time.time() * 1000)


def _ms_text(ms: int) -> str:
    return format_time(datetime.fromtimestamp(ms / 1000, UTC))


def monitoring(account: Account, org: str, series: tuple[str, str, str] | None, range_id: str) -> dict[str, Any]:
    uuid_argument(org)
    if series is None:
        answer = account.call("GET", f"/v1/monitoring/overview?{urlencode({'org': org})}")
        listed = {key: rows_of(answer, key) for key in ("devices", "vms", "sessions")}
        # the work VMs (KOR-29) below their sessions; a hub of before KOR-29 sends no list
        workvms = [] if answer.get("workvms") is None else rows_of(answer, "workvms")
        return {"org_id": org, **listed, "workvms": workvms}
    kind, entity, metric = series
    pattern = ENTITY_ID_RES.get(kind)
    if pattern is None:
        uuid_argument(entity)
    elif not pattern.fullmatch(entity):
        raise PlatformError("invalid_argument")
    start, end, step = series_window(range_id, now_ms())
    query = {
        "entity_kind": kind,
        "entity_id": entity,
        "metric": metric,
        "from": _ms_text(start),
        "to": _ms_text(end),
        "step": str(step),
    }
    answer = account.call("GET", f"/v1/monitoring/series?{urlencode(query)}")
    points = rows_of_points(answer)
    return {"org_id": org, "entity_kind": kind, "entity_id": entity, "metric": metric, "step": step, "points": points}


def rows_of_points(answer: dict[str, Any]) -> list[list[Any]]:
    points = answer.get("points")
    if not isinstance(points, list):
        raise unverified("response_invalid")
    return [
        point
        for point in points
        if isinstance(point, list)
        and len(point) == 2
        and isinstance(point[0], str)
        and (point[1] is None or (isinstance(point[1], int | float) and not isinstance(point[1], bool)))
    ]


# --- audit ---


def audit_row(row: dict[str, Any]) -> bool:
    """A row as `monitoring/audit.ts` `isAuditRow` allows it."""
    detail = row.get("detail")
    return (
        isinstance(row.get("seq"), int)
        and _time(row.get("at")) is not None
        and all(isinstance(row.get(name), str) for name in ("actor_kind", "action", "result"))
        and all(row.get(name) is None or isinstance(row.get(name), str) for name in AUDIT_NULLABLE)
        and isinstance(detail, dict)
        and all(value is None or isinstance(value, str | int | float | bool) for value in detail.values())
        and ("agent_id" not in row or isinstance(row["agent_id"], str))
    )


def _filters(args: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    if args.action is not None:
        if len(args.action) > ACTION_MAX or not ACTION_RE.fullmatch(args.action):
            raise PlatformError("invalid_argument")
        found["action"] = args.action
    for name in ("actor", "wg", "task", "session"):
        value = getattr(args, name)
        if value is not None:
            found[name] = uuid_argument(value)
    return found


def audit(account: Account, args: Any) -> dict[str, Any]:
    org = args.org
    filters = _filters(args)
    if org is None and (filters or args.verify):
        raise PlatformError("invalid_argument")
    if org is not None and args.verify:
        answer = account.call("GET", f"/v1/orgs/{segment(uuid_argument(org))}/audit/verify")
        head = answer.get("head")
        if not isinstance(answer.get("ok"), bool) or not isinstance(head, dict):
            raise unverified("response_invalid")
        return {"org_id": org, "ok": answer["ok"], "head": head, "checkpoint": answer.get("checkpoint")}
    query = {**filters, "limit": str(AUDIT_PAGE), **({"before": args.before} if args.before else {})}
    path = "/v1/me/audit" if org is None else f"/v1/orgs/{segment(uuid_argument(org))}/audit"
    answer = account.call("GET", f"{path}?{urlencode(query)}")
    following = answer.get("next_before")
    return {
        "org_id": org,
        "rows": [row for row in rows_of(answer, "rows") if audit_row(row)],
        "next_before": following if isinstance(following, str) else None,
    }


# --- commands ---


def _notifications_list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return notifications_list(account, args.unread, args.before)


def _notifications_read(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return notifications_read(account, args.id, args.all)


def _quotas(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return quotas(account)


def _monitoring(account: Account, args: Any, context: Context) -> dict[str, Any]:
    named = (args.entity_kind, args.entity_id, args.metric)
    if any(value is not None for value in named) and not all(value is not None for value in named):
        raise PlatformError("invalid_argument")
    series = (args.entity_kind, args.entity_id, args.metric) if args.entity_kind is not None else None
    return monitoring(account, args.org, series, args.range)


def _audit(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return audit(account, args)


def register(tree: Tree) -> None:
    group = tree.group("notifications", "the user's notifications")
    sub = tree.command(group, "list", _notifications_list, "notifications newest first")
    sub.add_argument("--unread", action="store_true", help="only unread ones")
    sub.add_argument("--before", help="next_before of the previous page")
    sub = tree.command(group, "read", _notifications_read, "mark notifications read")
    sub.add_argument("--id", action="append", default=[], help="notification ID, repeatable")
    sub.add_argument("--all", action="store_true", help="every notification")
    tree.command(tree.commands, "quotas", _quotas, "the subscription quotas of the user")
    text = "devices, VMs, sessions and work VMs of an org, or a series"
    sub = tree.command(tree.commands, "monitoring", _monitoring, text)
    sub.add_argument("--org", required=True)
    sub.add_argument("--entity-kind", choices=list(ENTITY_KINDS))
    sub.add_argument("--entity-id")
    sub.add_argument("--metric", choices=list(METRICS))
    sub.add_argument("--range", choices=list(RANGES), default="1h")
    sub = tree.command(tree.commands, "audit", _audit, "the account audit, or an org's audit (--org)")
    sub.add_argument("--org")
    sub.add_argument("--verify", action="store_true", help="verify the org's audit chain (org admins)")
    sub.add_argument("--action", help="an action or a group (`operation` matches `operation.*`)")
    for name in ("actor", "wg", "task", "session"):
        sub.add_argument(f"--{name}")
    sub.add_argument("--before", help="next_before of the previous page")
