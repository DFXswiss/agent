"""The measurement document M0.md states exactly what its raw data gives (tables, verdict, TUI series)."""

from __future__ import annotations

from pathlib import Path

from probelib.report import markdown_table, read_csv, summarize_path, summarize_series, verdict
from probelib.timeline import CLONE_MARKS, CLONE_STEPS, POOL_MARKS, POOL_STEPS, TOTAL_BUDGET_S, check_order, end_to_end

MEASUREMENTS = Path(__file__).resolve().parents[3] / "docs" / "platform" / "measurements"


def _runs(path: str, marks: tuple[str, ...]) -> list[dict[str, float]]:
    rows = read_csv(MEASUREMENTS / "m0-clone-path.csv", text_columns=("path", "hook_event", "hook_events", "failure"))
    assert not [row for row in rows if "failure" in row]
    runs = [{k: float(v) for k, v in row.items() if k in marks} for row in rows if row["path"] == path]
    for run in runs:
        check_order(run, marks)
    return runs


def test_clone_and_pool_tables_match_the_raw_data() -> None:
    text = (MEASUREMENTS / "M0.md").read_text(encoding="utf-8")
    for path, steps, marks in (("clone", CLONE_STEPS, CLONE_MARKS), ("pool", POOL_STEPS, POOL_MARKS)):
        runs = _runs(path, marks)
        assert len(runs) == 10
        table = markdown_table(summarize_path(runs, steps, total_budget_s=TOTAL_BUDGET_S[path]))
        assert table in text, path
    found = verdict([end_to_end(run) for run in _runs("clone", CLONE_MARKS)])
    assert found.go is True
    assert f"**{found.text}**" in text


def test_tui_table_matches_the_raw_data() -> None:
    text = (MEASUREMENTS / "M0.md").read_text(encoding="utf-8")
    rows = read_csv(MEASUREMENTS / "m0-tui-start.csv", text_columns=("variant",))
    series = []
    for variant in ("landlock", "plain"):
        for metric in ("paint_ms", "transcript_ms"):
            values = [float(row[metric]) for row in rows if row["variant"] == variant]
            assert len(values) == 10
            series.append(summarize_series(f"{variant}.{metric}", values, budget_s=None, reference="ms"))
    assert markdown_table(series) in text
