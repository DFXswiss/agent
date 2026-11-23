"""Distributions of the probe measurements (nearest-rank percentiles)."""

from __future__ import annotations

import pytest
from probelib.stats import Distribution, distribution, percentile


def test_percentile_is_nearest_rank() -> None:
    values = [5.0, 1.0, 4.0, 2.0, 3.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert percentile(values, 50) == 5.0
    assert percentile(values, 95) == 10.0
    assert percentile(values, 90) == 9.0
    assert percentile(values, 100) == 10.0
    assert percentile(values, 1) == 1.0
    assert percentile([2.5], 95) == 2.5


def test_percentile_of_twenty_values() -> None:
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 95) == 19.0


@pytest.mark.parametrize("q", [0, -1, 100.5])
def test_percentile_refuses_bad_ranks(q: float) -> None:
    with pytest.raises(ValueError, match="percentile"):
        percentile([1.0], q)


def test_percentile_refuses_no_values() -> None:
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


def test_distribution() -> None:
    found = distribution([3.0, 1.0, 2.0, 4.0])
    assert found == Distribution(n=4, min=1.0, median=2.5, p95=4.0, max=4.0)
    assert found.row(3) == {"n": 4, "min": 1.0, "median": 2.5, "p95": 4.0, "max": 4.0}
    assert distribution([1.23456]).row(2) == {"n": 1, "min": 1.23, "median": 1.23, "p95": 1.23, "max": 1.23}


def test_distribution_refuses_no_values() -> None:
    with pytest.raises(ValueError, match="no values"):
        distribution([])
