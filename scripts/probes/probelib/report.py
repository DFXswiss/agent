"""Summaries per step, the go/no-go rule of WP-M0-14, and CSV/Markdown output of the probes."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from probelib.stats import Distribution, distribution
from probelib.timeline import Step, end_to_end, step_durations

NONE = "-"
# 09-milestones.md M0 acceptance: a clone path above 9 s revises the M1 plan before M1 starts.
GO_THRESHOLD_S = 9.0
END_TO_END = "end_to_end"


@dataclass(frozen=True)
class StepSummary:
    name: str
    reference: str
    budget_s: float | None
    distribution: Distribution
    within: bool | None


@dataclass(frozen=True)
class Verdict:
    go: bool
    p95_s: float
    max_s: float
    threshold_s: float
    text: str


def summarize_series(name: str, values: Sequence[float], *, budget_s: float | None, reference: str) -> StepSummary:
    """One series against an optional budget (`within`: p95 at or below the budget)."""
    found = distribution(values)
    within = None if budget_s is None else found.p95 <= budget_s
    return StepSummary(name, reference, budget_s, found, within)


def summarize_path(
    runs: Sequence[Mapping[str, float]], steps: Sequence[Step], *, total_budget_s: float
) -> list[StepSummary]:
    """Per step and end to end; optional steps that no run reached are left out."""
    if not runs:
        raise ValueError("no runs to summarize")
    durations = [step_durations(run, steps) for run in runs]
    rows = []
    for step in steps:
        values = [found[step.name] for found in durations if step.name in found]
        if values:
            rows.append(summarize_series(step.name, values, budget_s=step.budget_s, reference=step.reference))
    totals = [end_to_end(run) for run in runs]
    rows.append(
        summarize_series(
            END_TO_END, totals, budget_s=total_budget_s, reference="05 §6.1 total without hub and credential choice"
        )
    )
    return rows


def verdict(clone_totals: Sequence[float], *, threshold_s: float = GO_THRESHOLD_S, failed: int = 0) -> Verdict:
    """Go when no run failed and the p95 of the clone path end to end is at most the threshold (WP-M0-14 rule).

    A failed run (timeout, refused prompt, failed session) never reached `working`; it counts as above the threshold.
    """
    if failed < 0:
        raise ValueError("failed runs cannot be negative")
    found = distribution(clone_totals)
    go = found.p95 <= threshold_s and failed == 0
    relation = "<=" if found.p95 <= threshold_s else ">"
    text = (
        f"{'Go' if go else 'No-Go'}: clone path p95 {found.p95:.2f} s {relation} {threshold_s:.2f} s"
        f" (max {found.max:.2f} s)"
    )
    if failed:
        text += f", {failed} failed run(s)"
    return Verdict(go=go, p95_s=found.p95, max_s=found.max, threshold_s=threshold_s, text=text)


def markdown_table(rows: Iterable[StepSummary]) -> str:
    """Seconds with three decimals; budget and verdict per row."""
    lines = [
        "| Step | n | min | median | p95 | max | Budget | Within | Reference |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        d = row.distribution
        budget = NONE if row.budget_s is None else f"{row.budget_s:.2f}"
        within = NONE if row.within is None else ("yes" if row.within else "no")
        values = " | ".join(f"{value:.3f}" for value in (d.min, d.median, d.p95, d.max))
        lines.append(f"| {row.name} | {d.n} | {values} | {budget} | {within} | {row.reference} |")
    return "\n".join(lines) + "\n"


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    """Raw data: one row per run, empty cells for marks a run did not reach."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n", extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))


def read_csv(path: Path, *, text_columns: Sequence[str] = ()) -> list[dict[str, float | str]]:
    """Rows of `write_csv` back; numbers as float, empty cells left out."""
    out: list[dict[str, float | str]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            row: dict[str, float | str] = {}
            for key, value in raw.items():
                if value == "":
                    continue
                row[key] = value if key in text_columns else float(value)
            out.append(row)
    return out
