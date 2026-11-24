"""5-minute rollups and retention of the metric samples (02-data-model.md section 2.8, 08-monitoring.md section 4).

`metric_sample` keeps the raw samples for 48 h, `metric_rollup` one row per entity and 5-minute bucket for 90 days.
A bucket starts at a multiple of 300 s since the epoch and holds the samples with `bucket <= ts < bucket + 300 s`.
Per bucket the rollup keeps

- gauges (`cpu_ratio`, memory, disk, `free_vm_slots`, `free_mem_bytes`): the average and the maximum of the non-null
  values (null when the bucket has none);
- counters (`net_rx_bytes`, `net_tx_bytes`) and `uptime_seconds`: the value of the newest sample that has one;
- `reachable`, `state` and `device_id`: the newest sample's value; `samples`: the number of samples.

`roll_up` is one pass of the job (`agent_hub.monitoring.jobs`), in one transaction: it locks the cursor row (one pass
at a time across hub processes), recomputes from the raw rows every bucket touched by a sample received since the
cursor (minus `LATE_COMMIT_SECONDS` for transactions that committed after a pass read the table), moves the cursor to
the start of its transaction and prunes raw rows older than 48 h and rollups older than 90 days. Recomputing is
idempotent; after an outage the pass catches up with everything received meanwhile. A bucket that already has a rollup
is only replaced while all its raw samples are still kept (bucket start within the raw retention): a late sample of an
older bucket would otherwise replace the complete rollup by one built from that sample alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

Connection = psycopg.Connection[Any]
BUCKET_SECONDS = 300
RAW_RETENTION = timedelta(hours=48)
ROLLUP_RETENTION = timedelta(days=90)
LATE_COMMIT_SECONDS = 300

GAUGES = (
    "cpu_ratio",
    "mem_used_bytes",
    "mem_limit_bytes",
    "disk_used_bytes",
    "disk_limit_bytes",
    "free_vm_slots",
    "free_mem_bytes",
)
LAST_VALUES = ("net_rx_bytes", "net_tx_bytes", "uptime_seconds")
# Integer gauges are stored rounded; the averages of `cpu_ratio` and `free_vm_slots` are `real` columns.
_REAL_AVERAGES = frozenset({"cpu_ratio", "free_vm_slots"})


def bucket_start(moment: datetime) -> datetime:
    """The start of the 5-minute bucket that holds `moment` (UTC)."""
    seconds = int(moment.timestamp() // BUCKET_SECONDS) * BUCKET_SECONDS
    return datetime.fromtimestamp(seconds, UTC)


def _aggregates() -> tuple[list[str], list[str]]:
    columns: list[str] = []
    values: list[str] = []
    for name in GAUGES:
        average = f"avg(s.{name})" if name in _REAL_AVERAGES else f"round(avg(s.{name}))::bigint"
        columns += [f"{name}_avg", f"{name}_max"]
        values += [average, f"max(s.{name})"]
    for name in LAST_VALUES:
        columns.append(name)
        values.append(f"(array_agg(s.{name} ORDER BY s.ts DESC) FILTER (WHERE s.{name} IS NOT NULL))[1]")
    return columns, values


_COLUMNS, _VALUES = _aggregates()
_UPDATED = ("device_id", "samples", *_COLUMNS, "reachable", "state")
ROLLUP = (
    "WITH touched AS (SELECT DISTINCT entity_kind, entity_id,"
    " to_timestamp(floor(extract(epoch FROM ts) / 300) * 300) AS bucket"
    " FROM hub.metric_sample WHERE received_at > (SELECT received_through FROM hub.metric_rollup_cursor WHERE id = 1)"
    " - make_interval(secs => %(late)s))"
    f" INSERT INTO hub.metric_rollup (entity_kind, entity_id, ts, {', '.join(_UPDATED)})"
    " SELECT s.entity_kind, s.entity_id, t.bucket, (array_agg(s.device_id ORDER BY s.ts DESC))[1], count(*),"
    f" {', '.join(_VALUES)},"
    " (array_agg(s.reachable ORDER BY s.ts DESC))[1], (array_agg(s.state ORDER BY s.ts DESC))[1]"
    " FROM touched t JOIN hub.metric_sample s ON s.entity_kind = t.entity_kind AND s.entity_id = t.entity_id"
    " AND s.ts >= t.bucket AND s.ts < t.bucket + interval '300 seconds'"
    " GROUP BY s.entity_kind, s.entity_id, t.bucket"
    f" ON CONFLICT (entity_kind, entity_id, ts) DO UPDATE SET"
    f" {', '.join(f'{name} = EXCLUDED.{name}' for name in _UPDATED)}"
    " WHERE hub.metric_rollup.ts >= %(complete)s"
)


@dataclass(frozen=True)
class RollupPass:
    """What one pass did: rollup rows written, raw rows and rollups pruned."""

    rolled_up: int
    samples_pruned: int
    rollups_pruned: int


def roll_up(connection: Connection, now: datetime) -> RollupPass:
    """One pass of the rollup and the retention (module docstring); `now` is the hub clock."""
    with connection.transaction():
        connection.execute(
            "INSERT INTO hub.metric_rollup_cursor (id, received_through) VALUES (1, '1970-01-01T00:00:00Z')"
            " ON CONFLICT (id) DO NOTHING"
        )
        started = connection.execute(
            "SELECT statement_timestamp() FROM hub.metric_rollup_cursor WHERE id = 1 FOR UPDATE"
        ).fetchone()
        assert started is not None  # the row exists: inserted above or by migration 0005
        complete = now - RAW_RETENTION
        rolled = connection.execute(ROLLUP, {"late": LATE_COMMIT_SECONDS, "complete": complete}).rowcount
        connection.execute("UPDATE hub.metric_rollup_cursor SET received_through = %s WHERE id = 1", started)
        samples = connection.execute("DELETE FROM hub.metric_sample WHERE ts < %s", (complete,)).rowcount
        rollups = connection.execute("DELETE FROM hub.metric_rollup WHERE ts < %s", (now - ROLLUP_RETENTION,)).rowcount
    return RollupPass(rolled_up=rolled, samples_pruned=samples, rollups_pruned=rollups)
