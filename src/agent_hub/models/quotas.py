"""`GET /v1/orgs/{org}/quotas`: the model access of an organization as far as readable (ADR-080 section 6, KOR-50).

`org_quotas(connection, org, user, now=..., window=..., platform=...)` answers, for a member of the org (the caller
checks `org:member`):

- `own`: the caller's own subscription - its quota windows (as `GET /v1/me/quotas`), since when it refuses the read
  (`unreadable`) and the latest observed limit (`limited`, `hub.quota_limit`).
- `operator`: `granted` when the verified platform log grants the organization the operator's subscriptions (the
  latest `org_operator_subscriptions`, ADR-079); only then `subscriptions`: the newest snapshot of every active
  platform device that is an active member of the organization (`hub.operator_quota`), ordered by device and name.
  `used_percent` / `resets_at` are those of the subscription's fullest window of the whole subscription
  (`five_hour`, `seven_day`; per-model windows do not count, ADR-080 section 2); null without one.
- `platform.available`: an online, active platform device is an active member of the organization, or one of the
  organization's devices holds a live per-device DFX key.
- `summary`: over the listed subscriptions - the own one (when the caller has any record of it: windows, an
  unreadable mark or a limit) and the operator's. The own subscription is exhausted while its latest limit has no
  release time or one in the future, or a window of the whole subscription reads at least 100 % with a reset in the
  future; an operator subscription by its state (`usable` and `exhausted` count, the others not). `next_release` is
  the earliest future release time of the exhausted ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.crypto.encoding import format_time, parse_time
from agent_hub.log.platform import PlatformState
from agent_hub.usage.routes import quota_rows

Connection = psycopg.Connection[Any]
WHOLE = ("five_hour", "seven_day")
FULL = 100.0


def _when(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


def _fullest(windows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The window of the whole subscription with the highest use (the first of a tie), None without one."""
    whole = [window for window in windows if window["window"] in WHOLE]
    return max(whole, key=lambda window: window["used_percent"], default=None)


def _own(connection: Connection, user: UUID, now: datetime) -> tuple[dict[str, Any], datetime | bool | None]:
    """The `own` part and its state: False (no record), None (usable) or the release time (exhausted)."""
    windows = quota_rows(connection, user)
    unreadable = connection.execute("SELECT since FROM hub.quota_unreadable WHERE owner_id = %s", (user,)).fetchone()
    limit = connection.execute(
        "SELECT at, until, readable FROM hub.quota_limit WHERE owner_id = %s", (user,)
    ).fetchone()
    part = {
        "windows": windows,
        "unreadable": None if unreadable is None else {"since": format_time(unreadable[0])},
        "limited": None
        if limit is None
        else {"at": format_time(limit[0]), "until": _when(limit[1]), "readable": limit[2]},
    }
    if not windows and unreadable is None and limit is None:
        return part, False
    full = [
        parse_time(window["resets_at"])
        for window in windows
        if window["window"] in WHOLE and window["used_percent"] >= FULL and window["resets_at"] is not None
    ]
    releases = [moment for moment in full if moment > now]
    if limit is not None and (limit[1] is None or limit[1] > now):
        releases.append(limit[1] or now)
    return part, max(releases) if releases else None


def _subscriptions(connection: Connection, org: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT q.snapshot FROM hub.operator_quota q JOIN hub.device d ON d.id = q.device_id AND d.kind = 'platform'"
        " AND d.state = 'active' JOIN hub.org_member m ON m.org_id = %s AND m.principal_id = d.id"
        " AND m.removed_seq IS NULL ORDER BY d.id",
        (org,),
    ).fetchall()
    found = []
    for (snapshot,) in rows:
        for entry in sorted(snapshot, key=lambda entry: entry["name"]):
            fullest = _fullest(entry["windows"])
            found.append(
                {
                    "label": entry["name"],
                    "state": entry["state"],
                    "readable": entry["readable"],
                    "used_percent": None if fullest is None else fullest["used_percent"],
                    "resets_at": None if fullest is None else fullest["resets_at"],
                    "limited_at": entry["limited_at"],
                    "until": entry["until"],
                    "fetched_at": entry["fetched_at"],
                }
            )
    return found


def _platform(connection: Connection, org: UUID, now: datetime, window: int) -> bool:
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM hub.org_member m JOIN hub.device d ON d.id = m.principal_id"
        " AND d.kind = 'platform' AND d.state = 'active' JOIN hub.device_presence p ON p.device_id = d.id"
        " AND p.last_heartbeat_at > %s WHERE m.org_id = %s AND m.removed_seq IS NULL)"
        " OR EXISTS (SELECT 1 FROM hub.dfx_key WHERE org_id = %s AND envelope IS NOT NULL AND revoked_at IS NULL)",
        (now - timedelta(seconds=window), org, org),
    ).fetchone()
    return bool(row is not None and row[0])


def org_quotas(
    connection: Connection, org: UUID, user: UUID, *, now: datetime, window: int, platform: PlatformState | None
) -> dict[str, Any]:
    """The answer of `GET /v1/orgs/{org}/quotas` (module docstring)."""
    own, own_state = _own(connection, user, now)
    granted = platform is not None and platform.operator_subscribed(str(org))
    subscriptions = _subscriptions(connection, org) if granted else []
    usable = 1 if own_state is None else 0
    releases: list[datetime] = []
    if isinstance(own_state, datetime):
        releases.append(own_state)
    exhausted = len(releases)
    for entry in subscriptions:
        if entry["state"] == "usable":
            usable += 1
        elif entry["state"] == "exhausted":
            exhausted += 1
            if entry["until"] is not None:
                releases.append(parse_time(entry["until"]))
    future = [moment for moment in releases if moment > now]
    return {
        "own": own,
        "operator": {"granted": granted, "subscriptions": subscriptions},
        "platform": {"available": _platform(connection, org, now, window)},
        "summary": {
            "usable": usable,
            "exhausted": exhausted,
            "next_release": _when(min(future)) if future else None,
        },
    }
