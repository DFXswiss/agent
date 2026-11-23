"""Sample series of the final PERF evaluation (PFM-01; 07-testing.md section 2.2, 09-milestones.md WP-M3-03).

A series is a list of measured values of one kind (`name`, e.g. `reconnect`, `perf05-health`) under a label (e.g.
`runner catch-up T1`). Rows are appended to `<artifacts>/perf-<name>.csv` (`iteration,label,index,value,unit`); the
summary is `{n, min, p50, p95, max}` with nearest-rank percentiles, the same rule as `perf.time_to_working`. A series
passes a limit when p95 and maximum stay below it.

The hub's request log (uvicorn access lines) and the PostgreSQL log of the run give the refusals under load: status
counts, the refused requests (429 and 5xx) by method and path (IDs replaced by `{id}`) and the number of deadlocks
PostgreSQL detected (each one aborts one transaction, which the hub retries once before answering 503).

    python -m perf.samples <csv> [--limit N]     # summary per series of an existing CSV
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

COLUMNS = ("iteration", "label", "index", "value", "unit")
_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_ACCESS_RE = re.compile(r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[0-9.]+" (?P<status>\d{3})')
_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DEADLOCK = "deadlock detected"


def nearest_rank(ordered: Sequence[float], share: float) -> float:
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def distribution(values: Sequence[float]) -> dict[str, Any]:
    """`{n, min, p50, p95, max}` (nearest rank, rounded to 3 decimals); None for an empty series."""
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(nearest_rank(ordered, 0.5), 3),
        "p95": round(nearest_rank(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


def passed(summary: dict[str, Any], limit: float | None) -> bool:
    """A non-empty series passes when p95 and maximum stay below `limit` (no limit: any non-empty series)."""
    if not summary["n"]:
        return False
    return limit is None or (summary["p95"] < limit and summary["max"] < limit)


def series_path(artifacts: Path, name: object) -> Path:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"series name must match {_NAME_RE.pattern}")
    return artifacts / f"perf-{name}.csv"


def append_series(path: Path, iteration: str, label: str, unit: str, values: Sequence[float]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(COLUMNS)
        for index, value in enumerate(values, start=1):
            writer.writerow([iteration, label, str(index), f"{value:.3f}", unit])


def read_series(path: Path) -> dict[str, list[float]]:
    """Values per series `<iteration> <label>`."""
    series: dict[str, list[float]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            series.setdefault(f"{row['iteration']} {row['label']}", []).append(float(row["value"]))
    return series


def since(text: str, lines: int) -> str:
    """The log text after its first `lines` lines (a count taken earlier marks the start of a measured window)."""
    return "".join(text.splitlines(keepends=True)[lines:])


def normalized_path(path: str) -> str:
    return _ID_RE.sub("{id}", path.split("?", 1)[0])


def hub_requests(log: str) -> dict[str, Any]:
    """Requests of the hub's access log: total, count per status, refused (429 and 5xx) per method, path and status."""
    statuses: Counter[str] = Counter()
    refused: Counter[str] = Counter()
    for match in _ACCESS_RE.finditer(log):
        status = match["status"]
        statuses[status] += 1
        if status == "429" or status.startswith("5"):
            refused[f"{match['method']} {normalized_path(match['path'])} {status}"] += 1
    return {"requests": sum(statuses.values()), "by_status": dict(sorted(statuses.items())), "refused": dict(refused)}


def deadlocks(log: str) -> int:
    return sum(1 for line in log.splitlines() if DEADLOCK in line)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m perf.samples", description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    parser.add_argument("--limit", type=float, default=None)
    args = parser.parse_args(argv)
    ok = True
    for label, values in read_series(args.csv).items():
        summary = distribution(values)
        good = passed(summary, args.limit)
        ok = ok and good
        print(
            f"{label}: n={summary['n']} min={summary['min']} p50={summary['p50']} p95={summary['p95']}"
            f" max={summary['max']} passed={good}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
