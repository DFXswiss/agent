"""`GET /v1/orgs/{org}/quotas`: the model access of an organization as far as readable (ADR-081 section 6, KOR-50).

`org_quotas(connection, org, user, now=..., window=..., platform=...)` answers, for a member of the org (the caller
checks `org:member`):

- `own`: the caller's own subscription - its quota windows (as `GET /v1/me/quotas`), since when it refuses the read
  (`unreadable`) and the latest observed limit (`limited`, `hub.quota_limit`).
- `operator`: `granted` when the verified platform log grants the organization the operator's subscriptions (the
  latest `org_operator_subscriptions`, ADR-079); only then `subscriptions`: the newest snapshot of every active
  platform device that is an active member of the organization (`hub.operator_quota`), ordered by device and name.
  `used_percent` / `resets_at` are those of the subscription's fullest window of the whole subscription
  (`five_hour`, `seven_day`; per-model windows do not count, ADR-081 section 2); null without one.
- `platform.available`: an online, active platform device is an active member of the organization, or one of the
  organization's devices holds a live per-device DFX key.
- `summary`: over the listed subscriptions - the own one (when the caller has any record of it: windows, an
  unreadable mark or a limit) and the operator's. The own subscription is exhausted while its latest limit has no
  release time or one in the future, or a window of the whole subscription reads at least 100 % with a reset in the
  future (`subscription_state`); an operator subscription by its state (`usable` and `exhausted` count, the others
  not). `next_release` is the earliest future release time of the exhausted ones.

The rule for one subscription is shared with `GET /v1/me/subscriptions` (ADR-091 section 3), which names it for every
subscription record of the caller (`subscription_usage`, `own_usage`):

- `binding_window(windows, now)`: the window that binds a subscription now - of its whole windows the one with the
  highest use, a window whose reset has passed counting 0 (it is free again), the week on a tie.
- `subscription_state(windows, now, ...)`: None while it is usable, else its release (above).
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


def _used_now(window: dict[str, Any], now: datetime) -> float:
    """The use of a window at `now`: 0 once its reset has passed."""
    resets = window["resets_at"]
    return 0 if resets is not None and parse_time(resets) <= now else window["used_percent"]


def binding_window(windows: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The window of the whole subscription that binds it now (module docstring), with its use at `now`; None without
    one. A window per model binds only its model (ADR-081 section 2)."""
    whole = [window for window in windows if window["window"] in WHOLE]
    found = max(whole, key=lambda window: (_used_now(window, now), window["window"] == "seven_day"), default=None)
    return None if found is None else {**found, "used_percent": _used_now(found, now)}


def subscription_state(
    windows: list[dict[str, Any]], now: datetime, *, limited: bool = False, until: datetime | None = None
) -> datetime | None:
    """None while a subscription is usable, else its release (module docstring): a window of the whole subscription at
    100 % or more with a reset in the future (released when the last of them resets), or a latest limit (`limited`)
    without a release (`until`, then `now`) or with one in the future."""
    full = [
        parse_time(window["resets_at"])
        for window in windows
        if window["window"] in WHOLE and window["used_percent"] >= FULL and window["resets_at"] is not None
    ]
    releases = [moment for moment in full if moment > now]
    if limited and (until is None or until > now):
        releases.append(until or now)
    return max(releases) if releases else None


def _usage(windows: list[dict[str, Any]], release: datetime | None, now: datetime) -> dict[str, Any]:
    bound = binding_window(windows, now)
    return {
        "usable": release is None,
        "binding": None if bound is None else bound["window"],
        "release": None if release is None or release <= now else format_time(release),
    }


def subscription_usage(windows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """`state` of a further subscription in `GET /v1/me/subscriptions`: whether it is usable, its binding window and
    the release of an exhausted one (null when unknown)."""
    return _usage(windows, subscription_state(windows, now), now)


def own_usage(connection: Connection, user: UUID, now: datetime) -> dict[str, Any] | None:
    """The same `state` of the caller's own (primary) subscription, exactly as the `summary` counts it (with its
    latest limit); None while there is no record of it."""
    part, state = _own(connection, user, now)
    return None if state is False else _usage(part["windows"], state if isinstance(state, datetime) else None, now)


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
    return part, subscription_state(windows, now, limited=limit is not None, until=None if limit is None else limit[1])


def _subscriptions(connection: Connection, org: UUID, now: datetime) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT q.snapshot FROM hub.operator_quota q JOIN hub.device d ON d.id = q.device_id AND d.kind = 'platform'"
        " AND d.state = 'active' JOIN hub.org_member m ON m.org_id = %s AND m.principal_id = d.id"
        " AND m.removed_seq IS NULL ORDER BY d.id",
        (org,),
    ).fetchall()
    found = []
    for (snapshot,) in rows:
        for entry in sorted(snapshot, key=lambda entry: entry["name"]):
            fullest = binding_window(entry["windows"], now)
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
    subscriptions = _subscriptions(connection, org, now) if granted else []
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
