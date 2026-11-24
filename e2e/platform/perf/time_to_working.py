"""Time to working (PERF-01, PERF-02; 07-testing.md section 5.3): `session.first_working_at - task.created_at`.

Both ends are hub clock values from the hub database. For every measured task the main session's transitions give the
per-transition breakdown: the hub receive time (`received_at`) and the device-reported time (`at`) of each start state,
as seconds after `task.created_at`. Rows are appended to the run's `perf-time-to-working.csv` (one header, one row per
task; `iteration` (`iter-<n>`) and `label` (e.g. `PERF-01 new org`) name the series); the summary of a series is the
number of runs, p95 (nearest rank), maximum and whether every run and the p95 stay below the limit of 10 s.
Each row also carries the scheduler's start estimate (`session.estimate_ms`, 03-api.md section 4.6.2), the value the
countdown started from (AUD-01 A-30).

    python -m perf.time_to_working <csv> [--limit 10]     # summary per series of an existing CSV
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LIMIT_S = 10.0
STATES = ("requested", "vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering", "working")
COLUMNS = (
    "iteration",
    "label",
    "task_id",
    "session_id",
    "created_at",
    "total_s",
    "estimate_ms",
    *(f"{state}_hub_s" for state in STATES),
    *(f"{state}_device_s" for state in STATES),
)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

Query = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class Run:
    iteration: str
    label: str
    task_id: str
    session_id: str
    created_at: str
    total_s: float | None
    hub_s: dict[str, float] = field(default_factory=dict)
    device_s: dict[str, float] = field(default_factory=dict)
    estimate_ms: int | None = None

    def row(self) -> list[str]:
        def cell(value: float | None) -> str:
            return "" if value is None else f"{value:.3f}"

        return [
            self.iteration,
            self.label,
            self.task_id,
            self.session_id,
            self.created_at,
            cell(self.total_s),
            "" if self.estimate_ms is None else str(self.estimate_ms),
            *(cell(self.hub_s.get(state)) for state in STATES),
            *(cell(self.device_s.get(state)) for state in STATES),
        ]


def runs_sql(task_ids: Sequence[str]) -> str:
    """One row per (task, start transition) of the first main session of each task."""
    bad = [task for task in task_ids if not _UUID_RE.fullmatch(task)]
    if bad or not task_ids:
        raise ValueError(f"task IDs must be UUIDs: {bad or 'none given'}")
    listed = ", ".join(f"'{task}'" for task in task_ids)
    return (
        "SELECT t.id::text AS task_id, s.id::text AS session_id, to_char(t.created_at AT TIME ZONE 'UTC',"
        ' \'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"\') AS created_at,'
        " extract(epoch FROM s.first_working_at - t.created_at)::float8 AS total_s, s.estimate_ms, st.state,"
        " extract(epoch FROM st.received_at - t.created_at)::float8 AS hub_s,"
        " extract(epoch FROM st.at - t.created_at)::float8 AS device_s"
        " FROM hub.task t"
        " JOIN LATERAL (SELECT * FROM hub.session x WHERE x.task_id = t.id AND x.depth = 0"
        " ORDER BY x.created_at LIMIT 1) s ON true"
        " LEFT JOIN hub.session_transition st ON st.session_id = s.id"
        f" WHERE t.id IN ({listed}) ORDER BY t.created_at, st.seq"
    )


def collect(query: Query, label: str, task_ids: Sequence[str], iteration: str = "") -> list[Run]:
    """The measured runs of `task_ids` (in creation order) from the hub database."""
    runs: dict[str, Run] = {}
    for row in query(runs_sql(task_ids)):
        run = runs.get(row["task_id"])
        if run is None:
            run = Run(
                iteration,
                label,
                row["task_id"],
                row["session_id"],
                row["created_at"],
                row["total_s"],
                estimate_ms=row.get("estimate_ms"),
            )
            runs[row["task_id"]] = run
        state = row["state"]
        if state in STATES and state not in run.hub_s:
            run.hub_s[state] = float(row["hub_s"])
            run.device_s[state] = float(row["device_s"])
    missing = [task for task in task_ids if task not in runs]
    if missing:
        raise ValueError(f"tasks without a main session: {', '.join(missing)}")
    return list(runs.values())


def write_csv(path: Path, runs: Iterable[Run]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(COLUMNS)
        for run in runs:
            writer.writerow(run.row())


def p95(values: Sequence[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def summary(totals: Sequence[float | None], limit: float = LIMIT_S) -> dict[str, Any]:
    """`{runs, p95_s, max_s, passed}`; a run that never reached `working` counts as failed (infinite)."""
    values = [math.inf if value is None else value for value in totals]
    if not values:
        return {"runs": 0, "p95_s": None, "max_s": None, "passed": False}
    top, high = p95(values), max(values)
    return {
        "runs": len(values),
        "p95_s": None if math.isinf(top) else round(top, 3),
        "max_s": None if math.isinf(high) else round(high, 3),
        "passed": high < limit and top < limit,
    }


def slowest_step(run: Run) -> tuple[str, float] | None:
    """The start transition that took longest (hub receive times), for the cause of a slow run."""
    previous = 0.0
    worst: tuple[str, float] | None = None
    for state in STATES:
        if state in run.hub_s:
            step = run.hub_s[state] - previous
            previous = run.hub_s[state]
            if worst is None or step > worst[1]:
                worst = (state, round(step, 3))
    return worst


def read_csv(path: Path) -> dict[str, list[float | None]]:
    """Totals per series: `<iteration> <label>` (a run with `--repeat` has one series per iteration)."""
    totals: dict[str, list[float | None]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = f"{row['iteration']} {row['label']}".strip()
            totals.setdefault(key, []).append(float(row["total_s"]) if row["total_s"] else None)
    return totals


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m perf.time_to_working", description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    parser.add_argument("--limit", type=float, default=LIMIT_S)
    args = parser.parse_args(argv)
    ok = True
    for label, totals in read_csv(args.csv).items():
        result = summary(totals, args.limit)
        ok = ok and result["passed"]
        print(f"{label}: runs={result['runs']} p95={result['p95_s']} max={result['max_s']} passed={result['passed']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
