"""Storing and reading the daily UI health counters (`hub.ui_health_daily`, migration 0012; ADR-063 section 5).

`record` adds one report: each view's counters are clamped to `LIMITS` and added to the row of `(day, version,
view)`; a view type the hub does not know is counted as `FALLBACK` (a newer web client may know more types), and a
version beyond `versions_per_day` distinct versions of the day is counted as `OTHER_VERSION`, so anonymous reports
cannot grow the table without bound. `rows` reads the rows of the last `days` days (today included), newest day
first, at most `ROWS_MAX` (`truncated` when cut). Nothing about the sender is stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import psycopg

from agent_hub.telemetry.views import FALLBACK, LIMITS, OTHER_VERSION, VIEWS

Connection = psycopg.Connection[Any]
ROWS_MAX = 10000
KNOWN = frozenset(VIEWS)


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


def stored_version(connection: Connection, day: date, version: str) -> str:
    """The version a report of `day` is counted under (module docstring)."""
    row = connection.execute(
        "SELECT count(DISTINCT version), bool_or(version = %s) FROM hub.ui_health_daily WHERE day = %s",
        (version, day),
    ).fetchone()
    assert row is not None
    known, present = row
    return version if present or known < LIMITS["versions_per_day"] else OTHER_VERSION


def record(connection: Connection, day: date, version: str, views: Mapping[str, Counts]) -> None:
    """Add one report to the counters of `day` (module docstring)."""
    stored = stored_version(connection, day, version)
    for name, counts in sorted(merged(views).items()):
        connection.execute(
            "INSERT INTO hub.ui_health_daily AS d (day, version, view, reports, blocks, blocked_ms, max_bucket)"
            " VALUES (%s, %s, %s, 1, %s, %s, %s) ON CONFLICT (day, version, view) DO UPDATE SET"
            " reports = d.reports + 1, blocks = d.blocks + EXCLUDED.blocks,"
            " blocked_ms = d.blocked_ms + EXCLUDED.blocked_ms,"
            " max_bucket = GREATEST(d.max_bucket, EXCLUDED.max_bucket)",
            (day, stored, name, counts.count, counts.blocked_ms, counts.max_bucket),
        )


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
