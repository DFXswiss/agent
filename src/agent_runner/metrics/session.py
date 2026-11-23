"""Session entries of a sample (08-monitoring.md sections 1 and 3, session column; R-13.4: subagent sessions are full
sessions and appear like main sessions).

The sessions are this device's local sessions on a VM (`runner.session`, `vm_id` set) that have not ended, plus the
ended ones for `RETENTION_S` (15 min) after `ended_at`, so that Prometheus series of a `stopped` session stay that long
and then disappear (cardinality rule of section 3). A session the guest reports but the store does not know is
ignored: a guest never speaks for sessions this device does not run on that VM (00-overview.md B4).

Values per session: the guest's answer for it; after the session ended its last answer (so the retention window
shows the final values); a running session the guest did not report has no values. `cpu_ratio` from the
`cpu_usage_usec` delta between two answers (1.0 = one core; none for the first answer or after a counter reset),
`runtime_seconds` from `created_at` to now or to `ended_at`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from agent_runner.metrics.parsers import GuestMetrics
from agent_runner.sessions.records import COLUMNS, FINAL, LocalSession

RETENTION_S = 15 * 60
USEC = 1_000_000


@dataclass(frozen=True)
class SessionEntry:
    session_id: str
    vm_id: str
    state: str
    runtime_seconds: int
    cpu_ratio: float | None
    values: Mapping[str, int | None] | None
    reachable: bool
    ended: bool


async def session_rows(db: Any, now: datetime) -> list[LocalSession]:
    """Local sessions on a VM: not ended, or ended less than `RETENTION_S` ago; oldest first."""
    query = (
        f"SELECT {COLUMNS} FROM runner.session WHERE vm_id IS NOT NULL"
        " AND (state <> ALL(%s) OR ended_at >= %s) ORDER BY created_at, id"
    )
    async with db.connection() as conn:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, (sorted(FINAL), now - timedelta(seconds=RETENTION_S)))
        return [LocalSession(**row) for row in await cursor.fetchall()]


class SessionTracker:
    """Last guest values and CPU baselines per session, across samples."""

    def __init__(self) -> None:
        self.last: dict[str, Mapping[str, int | None]] = {}
        self.cpu: dict[str, tuple[int, float]] = {}

    def entries(
        self, rows: Sequence[LocalSession], guests: Mapping[str, GuestMetrics | None], now: datetime, clock: float
    ) -> list[SessionEntry]:
        found: list[SessionEntry] = []
        for row in rows:
            assert row.vm_id is not None
            guest = guests.get(row.vm_id)
            reported = next((item for item in guest.sessions if item.session_id == row.id), None) if guest else None
            ratio: float | None = None
            if reported is not None:
                usage = reported.values["cpu_usage_usec"]
                assert usage is not None
                previous = self.cpu.get(row.id)
                if previous is not None and usage >= previous[0] and clock > previous[1]:
                    ratio = round((usage - previous[0]) / USEC / (clock - previous[1]), 4)
                self.cpu[row.id] = (usage, clock)
                self.last[row.id] = reported.values
            ended = row.state in FINAL
            until = row.ended_at if ended and row.ended_at is not None else now
            found.append(
                SessionEntry(
                    session_id=row.id,
                    vm_id=row.vm_id,
                    state=row.state,
                    runtime_seconds=max(int((until - row.created_at).total_seconds()), 0),
                    cpu_ratio=None if ended else ratio,
                    # A running session the guest did not report is not measured now: no stale values.
                    values=reported.values if reported is not None else (self.last.get(row.id) if ended else None),
                    reachable=reported is not None,
                    ended=ended,
                )
            )
        keep = {row.id for row in rows}
        self.last = {key: value for key, value in self.last.items() if key in keep}
        self.cpu = {key: value for key, value in self.cpu.items() if key in keep}
        return found
