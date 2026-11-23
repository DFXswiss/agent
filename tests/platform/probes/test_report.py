"""Summaries, go/no-go, CSV and Markdown output of the probes."""

from __future__ import annotations

from pathlib import Path

import pytest
from probelib.report import (
    StepSummary,
    Verdict,
    markdown_table,
    read_csv,
    summarize_path,
    summarize_series,
    verdict,
    write_csv,
)
from probelib.timeline import CLONE_STEPS, POOL_STEPS


def clone_run(offset: float) -> dict[str, float]:
    return {
        "begin": 0.0,
        "cloned": 0.05,
        "started": 5.2 + offset,
        "healthy": 5.6 + offset,
        "assigned": 5.65 + offset,
        "session_begin": 6.4 + offset,
        "session_created": 6.5 + offset,
        "credential_put": 6.55 + offset,
        "agent_starting": 6.56 + offset,
        "first_hook": 7.4 + offset,
        "prompt_accepted": 7.6 + offset,
    }


def test_summarize_clone_path() -> None:
    runs = [clone_run(offset / 30) for offset in range(10)]
    rows = summarize_path(runs, CLONE_STEPS, total_budget_s=8.5)
    by_name = {row.name: row for row in rows}
    assert list(by_name)[-1] == "end_to_end"
    assert by_name["vm_ready"].budget_s == 6.0
    assert by_name["vm_ready"].within is True
    assert by_name["end_to_end"].distribution.p95 == pytest.approx(6.85 + 0.3)
    assert by_name["end_to_end"].within is True
    assert by_name["boot"].within is None
    assert "answered" not in by_name


def test_summarize_reports_a_missed_budget() -> None:
    slow = [dict(clone_run(0), prompt_accepted=9.0)]
    rows = {row.name: row for row in summarize_path(slow, CLONE_STEPS, total_budget_s=8.5)}
    assert rows["prompt_accept"].within is False
    assert rows["end_to_end"].within is True
    rows = {row.name: row for row in summarize_path(slow, CLONE_STEPS, total_budget_s=8.0)}
    assert rows["end_to_end"].within is False


def test_summarize_needs_runs() -> None:
    with pytest.raises(ValueError, match="no runs"):
        summarize_path([], POOL_STEPS, total_budget_s=3.0)


def test_summarize_series() -> None:
    row = summarize_series("paint", [0.2, 0.1, 0.3], budget_s=None, reference="ref")
    assert row == StepSummary("paint", "ref", None, row.distribution, None)
    assert row.distribution.median == 0.2


@pytest.mark.parametrize(
    ("totals", "go"),
    [([8.0] * 9 + [9.0], True), ([8.0] * 9 + [9.01], False), ([8.0] * 19 + [9.5], True), ([9.2] * 20, False)],
)
def test_verdict_uses_the_p95_against_nine_seconds(totals: list[float], go: bool) -> None:
    found = verdict(totals)
    assert found.go is go
    assert found.threshold_s == 9.0
    assert found.text.startswith("Go" if go else "No-Go")


def test_verdict_text() -> None:
    assert verdict([5.0, 6.0]) == Verdict(
        go=True, p95_s=6.0, max_s=6.0, threshold_s=9.0, text="Go: clone path p95 6.00 s <= 9.00 s (max 6.00 s)"
    )
    assert verdict([9.5], threshold_s=10.0).text == "Go: clone path p95 9.50 s <= 10.00 s (max 9.50 s)"
    assert verdict([9.5]).text == "No-Go: clone path p95 9.50 s > 9.00 s (max 9.50 s)"


def test_verdict_with_failed_runs() -> None:
    found = verdict([5.0, 6.0], failed=1)
    assert found.go is False
    assert found.text == "No-Go: clone path p95 6.00 s <= 9.00 s (max 6.00 s), 1 failed run(s)"
    with pytest.raises(ValueError, match="negative"):
        verdict([5.0], failed=-1)


def test_markdown_table() -> None:
    rows = summarize_path([clone_run(0), clone_run(0.1)], CLONE_STEPS[:1], total_budget_s=8.5)
    text = markdown_table(rows)
    lines = text.splitlines()
    assert lines[0] == "| Step | n | min | median | p95 | max | Budget | Within | Reference |"
    assert lines[2] == "| clone | 2 | 0.050 | 0.050 | 0.050 | 0.050 | - | - | 05 §3.2: limactl clone (APFS) |"
    assert lines[3].startswith("| end_to_end | 2 | 6.850 | 6.900 | 6.950 | 6.950 | 8.50 | yes |")
    slow = summarize_path([dict(clone_run(0), prompt_accepted=12.0)], CLONE_STEPS[:1], total_budget_s=8.5)
    assert "| 8.50 | no |" in markdown_table(slow)


def test_csv_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "runs.csv"
    write_csv(path, ["path", "run", "begin", "answered"], [{"path": "clone", "run": 1, "begin": 0.0}])
    assert path.read_text(encoding="utf-8") == "path,run,begin,answered\nclone,1,0.0,\n"
    assert read_csv(path, text_columns=("path",)) == [{"path": "clone", "run": 1.0, "begin": 0.0}]


def test_csv_refuses_unknown_columns(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="extra"):
        write_csv(tmp_path / "x.csv", ["a"], [{"a": 1, "extra": 2}])
