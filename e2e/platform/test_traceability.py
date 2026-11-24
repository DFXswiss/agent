"""Traceability test (R-P.4, docs/platform/spec/01-requirements.md "Coverage check").

Every requirement has at least one E2E or gate test; every test it names exists (an E2E/PERF/LIVE tag in a spec
title, or the evidence of a gate) or belongs to a later milestone; no spec names an unknown test or requirement;
`traceability.json` mirrors `01-requirements.md` row by row (153 requirements).

    python e2e/platform/test_traceability.py [--milestone N] [--agent <agent checkout>]

`--milestone N` additionally requires every test and requirement due up to milestone N (09-milestones.md) to exist.
Under pytest (`make platform-ci`) the check runs for milestone `E2E_TRACEABILITY_MILESTONE` (default 3, the last
accepted milestone); the agent checkout is checked when `AGENT_RUNNER_SRC` names one.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

SUITE = Path(__file__).resolve().parent
if str(SUITE) not in sys.path:
    sys.path.insert(0, str(SUITE))

from orchestrator.traceability import MAX_MILESTONE, Report, check_suite  # noqa: E402


def run(milestone: int | None, agent: Path | None) -> Report:
    return check_suite(SUITE, milestone=milestone, agent=agent)


def print_report(report: Report, milestone: int | None) -> None:
    scope = f"milestone {milestone}" if milestone is not None else "structure"
    for note in report.notes:
        print(f"note: {note}")
    for problem in report.problems:
        print(f"PROBLEM: {problem}")
    for req_id, tests in report.due.items():
        print(f"requirement {req_id}: " + (f"green ({', '.join(tests)})" if tests else "RED (no test present)"))
    summary = f"traceability ({scope}): {'green' if report.ok else 'RED'}, {len(report.written)} spec test IDs written"
    if milestone is not None:
        green = sum(1 for tests in report.due.values() if tests)
        summary += f", {green} of {len(report.due)} due requirements green"
    print(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--milestone", type=int, choices=range(MAX_MILESTONE + 1))
    parser.add_argument("--agent", type=Path, help="agent checkout (gate evidence of the agent repository)")
    args = parser.parse_args(argv)
    report = run(args.milestone, args.agent)
    print_report(report, args.milestone)
    return 0 if report.ok else 1


ACCEPTED_MILESTONE = 3
"""The last accepted milestone (ACC-M3); the gate checks every requirement and test due up to it."""


def test_traceability() -> None:
    milestone = int(os.environ.get("E2E_TRACEABILITY_MILESTONE", str(ACCEPTED_MILESTONE)))
    agent = os.environ.get("AGENT_RUNNER_SRC")
    report = run(milestone, Path(agent) if agent else None)
    assert report.ok, "\n".join(report.problems)


def test_command_line(capsys: object) -> None:
    assert main(["--milestone", "0"]) == 0
    assert main([]) == 0
    # the exit code follows the report: milestone 3 is complete only once every M3 test exists (PERF-05, PFM-01)
    assert main(["--milestone", "3"]) == (0 if run(3, None).ok else 1)


if __name__ == "__main__":
    sys.exit(main())
