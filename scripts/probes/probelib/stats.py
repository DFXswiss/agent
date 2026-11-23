"""Distributions: min, median, p95 (nearest rank) and max of a measurement series."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Distribution:
    n: int
    min: float
    median: float
    p95: float
    max: float

    def row(self, digits: int) -> dict[str, float | int]:
        """The values rounded to `digits` decimals (for tables and JSON)."""
        return {
            "n": self.n,
            "min": round(self.min, digits),
            "median": round(self.median, digits),
            "p95": round(self.p95, digits),
            "max": round(self.max, digits),
        }


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least `q` % of the values at or below it.

    With 10 runs the p95 is the maximum, with 20 runs the 19th value - the conservative reading of the PERF rules.
    """
    if not values:
        raise ValueError("percentile of no values")
    if not 0 < q <= 100:
        raise ValueError(f"percentile rank {q} outside (0, 100]")
    ordered = sorted(values)
    rank = math.ceil(q / 100 * len(ordered))
    return ordered[rank - 1]


def distribution(values: Sequence[float]) -> Distribution:
    if not values:
        raise ValueError("distribution of no values")
    return Distribution(
        n=len(values),
        min=min(values),
        median=statistics.median(values),
        p95=percentile(values, 95),
        max=max(values),
    )
