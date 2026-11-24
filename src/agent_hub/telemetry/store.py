"""Storing and reading the daily UI health counters (`hub.ui_health_daily`, migration 0030; ADR-086 section 5).

`record` adds one report: each view's counters are clamped to `LIMITS` and added to the row of `(day, version,
view)`; a view type the hub does not know is counted as `FALLBACK` (a newer web client may know more types), and a
version other than the ones the hub serves (`SERVED`: the hub's own version, which its web client reports as
`config.version`) is counted as `OTHER_VERSION` - a tab of an older build or an invented version - so anonymous
reports cannot grow the table beyond (days x 2 versions x views). The reports of one day are added under a
transaction-scoped advisory lock of that day (`LOCK_ID`, `day`), so concurrent reports never race. `purge` deletes
the days older than `RETENTION_DAYS` (90, the longest range `rows` reads; job `telemetry.ui_health_purge`). `rows`
reads the rows of the last `days` days (today included), newest day first, at most `ROWS_MAX` (`truncated` when cut).
Nothing about the sender is stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg

from agent_hub import __version__
from agent_hub.telemetry.views import FALLBACK, LIMITS, OTHER_VERSION, VIEWS

Connection = psycopg.Connection[Any]
ROWS_MAX = 10000
RETENTION_DAYS = 90
KNOWN = frozenset(VIEWS)
SERVED = frozenset({__version__})
"""The versions the hub serves a web client of (module docstring)."""
LOCK_ID = 0x75695F68
"""First key of the per-day advisory lock (`ui_h`)."""


@dataclass(frozen=True)
class Counts:
    """The counters of one view in one report (not yet clamped)."""

    count: int
    blocked_ms: int
    max_bucket: int


def clamp(counts: Counts) -> Counts:
    """The counters within `LIMITS`."""
    return Counts(
        min(counts.count, LIMITS["count"]),
        min(counts.blocked_ms, LIMITS["blocked_ms"]),
        min(counts.max_bucket, LIMITS["max_bucket"]),
    )


def merged(views: Mapping[str, Counts]) -> dict[str, Counts]:
    """The views of a report by known type (unknown types summed into `FALLBACK`), each clamped."""
    found: dict[str, Counts] = {}
    for name, counts in views.items():
        key = name if name in KNOWN else FALLBACK
        before = found.get(key, Counts(0, 0, 0))
        found[key] = clamp(
            Counts(
                before.count + counts.count,
                before.blocked_ms + counts.blocked_ms,
                max(before.max_bucket, counts.max_bucket),
            )
        )
    return found


def stored_version(version: str) -> str:
    """The version a report is counted under (module docstring)."""
    return version if version in SERVED else OTHER_VERSION


def record(connection: Connection, day: date, version: str, views: Mapping[str, Counts]) -> None:
    """Add one report to the counters of `day` (module docstring)."""
    stored = stored_version(version)
    connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_ID, day.toordinal()))
    for name, counts in sorted(merged(views).items()):
        connection.execute(
            "INSERT INTO hub.ui_health_daily AS d (day, version, view, reports, blocks, blocked_ms, max_bucket)"
            " VALUES (%s, %s, %s, 1, %s, %s, %s) ON CONFLICT (day, version, view) DO UPDATE SET"
            " reports = d.reports + 1, blocks = d.blocks + EXCLUDED.blocks,"
            " blocked_ms = d.blocked_ms + EXCLUDED.blocked_ms,"
            " max_bucket = GREATEST(d.max_bucket, EXCLUDED.max_bucket)",
            (day, stored, name, counts.count, counts.blocked_ms, counts.max_bucket),
        )


def purge(connection: Connection, today: date) -> int:
    """Delete the days older than `RETENTION_DAYS`; the number of rows deleted."""
    cur = connection.execute(
        "DELETE FROM hub.ui_health_daily WHERE day <= %s", (today - timedelta(days=RETENTION_DAYS),)
    )
    return cur.rowcount


def rows(connection: Connection, today: date, days: int) -> dict[str, Any]:
    """The counters of the last `days` days, newest day first, then by version and view (module docstring)."""
    found = connection.execute(
        "SELECT day, version, view, reports, blocks, blocked_ms, max_bucket FROM hub.ui_health_daily"
        " WHERE day > %s ORDER BY day DESC, version, view LIMIT %s",
        (today - timedelta(days=days), ROWS_MAX + 1),
    ).fetchall()
    answer: dict[str, Any] = {
        "from": (today - timedelta(days=days - 1)).isoformat(),
        "rows": [
            {
                "day": day.isoformat(),
                "version": version,
                "view": view,
                "reports": int(reports),
                "count": int(blocks),
                "blocked_ms": int(blocked_ms),
                "max_bucket": int(max_bucket),
            }
            for day, version, view, reports, blocks, blocked_ms, max_bucket in found[:ROWS_MAX]
        ],
    }
    if len(found) > ROWS_MAX:
        answer["truncated"] = True
    return answer
